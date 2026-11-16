"""Downloading a default's missing models into the person's own ComfyUI: one binding confirmation."""
from __future__ import annotations

import errno
import hashlib
import http.server
import os
import re
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from astrid.core.generation.asset_gaps import (
    AttachedDownloadRefused, _fetch_into_folder, apply_attached_model_download,
    plan_attached_model_download, render_attached_download_plan,
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
        ranged = self.headers.get("Range")
        self.server.ranges.append((self.path, ranged))
        start = int(re.fullmatch(r"bytes=(\d+)-", ranged).group(1)) if ranged else 0
        if start:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(blob) - 1}/{len(blob)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(blob) - start))
        self.end_headers()
        stop = self.server.cut_at.get(self.path, len(blob))
        if self.path in self.server.truncated:
            stop = min(stop, start + 3)
        self.wfile.write(blob[start:stop])
        self.wfile.flush()
        if stop < len(blob):
            # the promised length never arrives: an interrupted download
            self.close_connection = True

    def log_message(self, *args):
        pass


@pytest.fixture
def file_server(monkeypatch):
    from astrid.core.generation import asset_gaps
    real = asset_gaps._token_safe_source
    monkeypatch.setattr(asset_gaps, "_token_safe_source",
                        lambda url: real(url) or url.startswith("http://127.0.0.1:"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.blobs, server.requested, server.truncated = dict(BLOBS), [], set()
    server.ranges, server.cut_at = [], {}  # (path, Range header) per request; path -> byte offset where the body is cut
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


def test_an_interrupted_download_keeps_only_its_resumable_part(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)
    file_server.truncated.add("/vae.bin")
    with pytest.raises(AttachedDownloadRefused):
        _apply(comfy, file_server, plan["plan_sha256"])
    assert not (root / "vae" / "ae.safetensors").exists()
    # complete, verified files plus the interrupted file's resumable part; no temp or receipt litter under models
    left = _files(root)
    assert "vae/.ae.safetensors.astrid-resume" in left
    assert (root / "vae" / ".ae.safetensors.astrid-resume").read_bytes() == BLOBS["/vae.bin"][:3]
    assert [n for n in left if not n.endswith(".astrid-resume")] == [
        "diffusion_models/z_image_turbo_bf16.safetensors", "text_encoders/qwen_3_4b.safetensors"]
    assert not (root / ".vibecomfy").exists()
    for name in left:
        if name.endswith(".astrid-resume"):
            continue
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
    # R4.9: a local file the server does not list changes the plan, so apply refuses before any write; the publish-time file_exists refusal is covered by test_without_hard_links_an_existing_file_is_never_overwritten
    assert caught.value.code == "plan_changed"
    assert existing.read_bytes() == b"the person's own file"
    assert _files(root) == ["vae/ae.safetensors"]  # nothing else was written in that folder
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


def test_the_real_default_plans_its_three_declared_files_and_nothing_manual(comfy, models):
    root, _ = models
    for loader, field in (("UNETLoader", "unet_name"), ("CLIPLoader", "clip_name"), ("VAELoader", "vae_name")):
        comfy.info[loader]["input"]["required"][field] = [[]]
    plan = plan_attached_model_download(comfy.client, "qwen-image-2.1-turbo", "t2i")
    assert {f["file"]: (f["bytes"], f["folder"]) for f in plan["files"]} == {
        "qwen_image_2.1_turbo_int8_convrot.safetensors": (7256787829, str(root / "diffusion_models")),
        "qwen3vl_8b_int8_convrot.safetensors": (9350798360, str(root / "text_encoders")),
        "qwen_image_2.1_vae_bf16.safetensors": (675509688, str(root / "vae")),
    }
    assert plan["total_bytes"] == 7256787829 + 9350798360 + 675509688
    assert plan["manual"] == []
    assert all(f["url"].startswith("https://huggingface.co/") for f in plan["files"])
    assert _files(root) == []


@pytest.mark.parametrize("url", ["https://example.com/ae.bin", "http://huggingface.co/ae.bin",
                                 "https://huggingface.co.evil.test/ae.bin"])
def test_a_source_off_huggingface_or_over_http_is_manual_with_a_reason(comfy, models, url):
    root, _ = models
    spec = ModelSpec("ae.safetensors", None, None, None, url, "vae/ae.safetensors", 9, "0" * 64)
    plan = plan_attached_model_download(comfy.client, "z-image", "t2i", sources={"vae/ae.safetensors": spec})
    assert plan["files"] == []
    vae = next(m for m in plan["manual"] if m["role"] == "vae")
    assert "huggingface.co" in vae["reason"]
    text = render_attached_download_plan(plan)
    assert "huggingface.co" in text and "put " in text.lower() and str(root / "vae") in text


def _no_hard_links(monkeypatch):
    """A filesystem without hard links (FAT, exFAT, some network mounts) refuses every link."""
    def refuse(src, dst, *args, **kwargs):
        raise OSError(errno.EPERM, "hard links are not supported on this filesystem", os.fspath(dst))
    monkeypatch.setattr(os, "link", refuse)


def test_a_folder_without_hard_links_still_gets_every_file(comfy, models, file_server, monkeypatch):
    root, _ = models
    _no_hard_links(monkeypatch)
    plan = _plan(comfy, file_server)
    result = _apply(comfy, file_server, plan["plan_sha256"])
    assert _files(root) == ["diffusion_models/z_image_turbo_bf16.safetensors",
                            "text_encoders/qwen_3_4b.safetensors", "vae/ae.safetensors"]
    for key, (name, path) in WANTED.items():
        assert (root / key / name).read_bytes() == BLOBS[path]
    assert result["downloaded"] and result["total_bytes"] == TOTAL


def test_without_hard_links_an_existing_file_is_never_overwritten(comfy, models, file_server, monkeypatch):
    root, _ = models
    _no_hard_links(monkeypatch)
    plan = _plan(comfy, file_server)
    item = next(f for f in plan["files"] if f["file"] == "ae.safetensors")
    existing = root / "vae" / "ae.safetensors"
    existing.write_bytes(b"the person's own file")
    with pytest.raises(AttachedDownloadRefused) as caught:
        _fetch_into_folder(item, _http())
    assert caught.value.code == "file_exists"
    assert existing.read_bytes() == b"the person's own file"
    assert _files(root) == ["vae/ae.safetensors"]  # no staged partial, no placeholder
    assert not (root / ".vibecomfy").exists()


def test_a_failed_publish_without_hard_links_leaves_no_placeholder(comfy, models, file_server, monkeypatch):
    root, _ = models
    _no_hard_links(monkeypatch)
    plan = _plan(comfy, file_server)
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if os.fspath(dst).endswith("ae.safetensors"):
            raise OSError(errno.EIO, "the drive went away", os.fspath(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(AttachedDownloadRefused) as caught:  # the failure is reported with what finished before it
        _apply(comfy, file_server, plan["plan_sha256"])
    assert isinstance(caught.value.__cause__, OSError)
    assert "Its partial download is kept" not in str(caught.value)  # a verified file is never left as a partial
    assert not (root / "vae" / "ae.safetensors").exists()
    assert not [n for n in _files(root) if n.startswith("vae/")]


def test_the_plan_records_whether_each_folder_takes_hard_links(comfy, models, file_server, monkeypatch):
    root, _ = models
    assert all(f["hard_links"] is True for f in _plan(comfy, file_server)["files"])
    _no_hard_links(monkeypatch)
    assert all(f["hard_links"] is False for f in _plan(comfy, file_server)["files"])
    assert _files(root) == []  # the probe file is removed again


def test_a_server_file_the_local_folder_lacks_refuses_that_folder(comfy, models, file_server):
    root, _ = models
    comfy.info["VAELoader"]["input"]["required"]["vae_name"] = [["other_vae.safetensors"]]
    plan = _plan(comfy, file_server)
    vae = next(m for m in plan["manual"] if m["role"] == "vae")
    assert "isn't the one on this computer" in vae["reason"]
    assert "ae.safetensors" not in [f["file"] for f in plan["files"]]
    assert sorted(f["file"] for f in plan["files"]) == ["qwen_3_4b.safetensors", "z_image_turbo_bf16.safetensors"]
    assert _files(root) == []


def test_a_local_model_file_the_server_does_not_list_refuses_that_folder(comfy, models, file_server):
    root, _ = models
    (root / "vae" / "stray.safetensors").write_bytes(b"stray")
    plan = _plan(comfy, file_server)
    vae = next(m for m in plan["manual"] if m["role"] == "vae")
    assert "isn't the one on this computer" in vae["reason"]
    assert "ae.safetensors" not in [f["file"] for f in plan["files"]]
    assert _files(root) == ["vae/stray.safetensors"]


def test_matching_folder_listings_across_legacy_and_canonical_folders_plan_normally(comfy, models, file_server):
    root, _ = models
    (root / "unet" / "shared.safetensors").write_bytes(b"shared")  # legacy folder, same name the server lists
    comfy.info["UNETLoader"]["input"]["required"]["unet_name"] = [["shared.safetensors"]]
    plan = _plan(comfy, file_server)
    assert not [m for m in plan["manual"] if "isn't the one" in m.get("reason", "")]
    assert sorted(f["file"] for f in plan["files"]) == ["ae.safetensors", "qwen_3_4b.safetensors",
                                                        "z_image_turbo_bf16.safetensors"]


def test_empty_listings_and_a_stray_non_model_file_plan_normally(comfy, models, file_server):
    root, _ = models
    (root / "vae" / "notes.txt").write_text("not a model")
    plan = _plan(comfy, file_server)
    assert not [m for m in plan["manual"] if "isn't the one" in m.get("reason", "")]
    assert "ae.safetensors" in [f["file"] for f in plan["files"]]


def test_a_file_that_appears_before_the_hard_link_is_never_overwritten(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)  # hard links work here: publish takes the os.link path
    item = next(f for f in plan["files"] if f["file"] == "ae.safetensors")
    assert item["hard_links"] is True
    existing = root / "vae" / "ae.safetensors"
    existing.write_bytes(b"appeared while downloading")
    with pytest.raises(AttachedDownloadRefused) as caught:
        _fetch_into_folder(item, _http())
    assert caught.value.code == "file_exists"
    assert existing.read_bytes() == b"appeared while downloading"
    assert _files(root) == ["vae/ae.safetensors"]  # the staged download is cleaned up


def test_a_file_of_the_old_staging_name_is_never_deleted(comfy, models, file_server):
    """The person's own `<model>.astrid-partial` survives a successful and a failed download byte for byte."""
    root, _ = models
    plan = _plan(comfy, file_server)
    item = next(f for f in plan["files"] if f["file"] == "ae.safetensors")
    theirs = root / "vae" / "ae.safetensors.astrid-partial"
    theirs.write_bytes(b"the person's own file, not Astrid's")
    final = root / "vae" / "ae.safetensors"

    _fetch_into_folder(item, _http())
    assert final.read_bytes() == BLOBS["/vae.bin"]
    assert theirs.read_bytes() == b"the person's own file, not Astrid's"
    assert sorted(p.name for p in (root / "vae").iterdir()) == ["ae.safetensors", "ae.safetensors.astrid-partial"]

    final.unlink()
    file_server.blobs["/vae.bin"] = b"vae-BYTES"  # same size, other bytes: the checksum fails
    with pytest.raises(Exception):
        _fetch_into_folder(item, _http())
    assert not final.exists()
    assert theirs.read_bytes() == b"the person's own file, not Astrid's"
    assert sorted(p.name for p in (root / "vae").iterdir()) == ["ae.safetensors.astrid-partial"]
    assert not (root / ".vibecomfy").exists()


LOADERS = {"diffusion_models": ("UNETLoader", "unet_name"), "text_encoders": ("CLIPLoader", "clip_name"),
           "vae": ("VAELoader", "vae_name")}


def _list_on_comfy(comfy, folder, name):
    """ComfyUI rescans its model folders after a publish, so the finished file is one the server lists."""
    loader, field = LOADERS[Path(folder).name]
    comfy.info[loader]["input"]["required"][field] = [[name]]


def _url_path(url):
    return urlsplit(url).path


def test_a_failed_file_says_what_finished_and_what_did_not(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)
    first, second, third = plan["files"]
    file_server.truncated.add(_url_path(second["url"]))
    with pytest.raises(AttachedDownloadRefused) as caught:
        _apply(comfy, file_server, plan["plan_sha256"])
    text = str(caught.value)
    assert second["file"] in text and f"Finished in this run: {first['file']}." in text
    assert f"Not downloaded: {third['file']}." in text and caught.value.code == "download_failed"
    assert "Nothing was written" not in text and "resumes it" in text
    assert (Path(first["folder"]) / first["file"]).read_bytes() == BLOBS[_url_path(first["url"])]


def test_the_same_digest_then_fetches_only_the_rest(comfy, models, file_server, capsys):
    root, _ = models
    plan = _plan(comfy, file_server)
    first, second, third = plan["files"]
    file_server.truncated.add(_url_path(second["url"]))
    with pytest.raises(AttachedDownloadRefused):
        _apply(comfy, file_server, plan["plan_sha256"])
    _list_on_comfy(comfy, first["folder"], first["file"])  # the finished file is now listed by ComfyUI
    file_server.truncated.clear()
    asked = len(file_server.requested)
    again = _plan(comfy, file_server)
    assert again["plan_sha256"] == plan["plan_sha256"]
    assert [p["file"] for p in again["present"]] == [first["file"]]
    assert "already on your ComfyUI" in render_attached_download_plan(again)
    capsys.readouterr()
    result = _apply(comfy, file_server, plan["plan_sha256"])
    assert [d["file"] for d in result["downloaded"]] == [second["file"], third["file"]]
    assert result["downloaded"][0]["resumed_from_bytes"] == 3  # the interrupted file continued, not restarted
    assert file_server.requested[asked:] == [_url_path(second["url"]), _url_path(third["url"])]
    for f in (second, third):
        assert (Path(f["folder"]) / f["file"]).read_bytes() == BLOBS[_url_path(f["url"])]
    assert not [n for n in _files(root) if n.endswith(".astrid-resume")]
    captured = capsys.readouterr()
    assert captured.out == ""  # progress is on stderr only
    assert f"Downloaded {second['file']}" in captured.err and f"Downloaded {third['file']}" in captured.err


def test_a_truncated_large_file_resumes_with_a_range_request(comfy, models, file_server, monkeypatch, capsys):
    from astrid.core.generation import asset_gaps
    root, _ = models
    monkeypatch.setattr(asset_gaps, "_PROGRESS_STEP", 64 << 10)
    big = bytes(range(256)) * 1024  # 256 KiB
    file_server.blobs["/big.bin"] = big
    sources = _sources(file_server)
    base = f"http://127.0.0.1:{file_server.server_port}"
    sources["vae/ae.safetensors"] = ModelSpec("ae.safetensors", None, None, None, base + "/big.bin",
                                              "vae/ae.safetensors", len(big), hashlib.sha256(big).hexdigest())
    plan = plan_attached_model_download(comfy.client, "z-image", "t2i", sources=sources)

    def apply():
        return apply_attached_model_download(comfy.client, "z-image", "t2i", plan_sha256=plan["plan_sha256"],
                                             sources=sources, http_client=_http())

    file_server.cut_at["/big.bin"] = 100_000
    with pytest.raises(AttachedDownloadRefused):
        apply()
    assert (root / "vae" / ".ae.safetensors.astrid-resume").read_bytes() == big[:100_000]
    for f in plan["files"]:
        if (Path(f["folder"]) / f["file"]).exists():
            _list_on_comfy(comfy, f["folder"], f["file"])
    file_server.cut_at.clear()
    file_server.requested.clear()
    file_server.ranges.clear()
    capsys.readouterr()
    result = apply()
    assert [r for path, r in file_server.ranges if path == "/big.bin"] == ["bytes=100000-"]
    assert file_server.requested == ["/big.bin"]
    assert (root / "vae" / "ae.safetensors").read_bytes() == big
    assert result["downloaded"][-1]["resumed_from_bytes"] == 100_000
    err = capsys.readouterr().err
    assert "ae.safetensors: " in err and " of " in err  # periodic progress
    assert "resumed from 100000 bytes" in err


def test_a_changed_url_or_sha256_still_refuses_with_plan_changed(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)
    first, second, third = plan["files"]
    file_server.truncated.add(_url_path(second["url"]))
    with pytest.raises(AttachedDownloadRefused):
        _apply(comfy, file_server, plan["plan_sha256"])
    _list_on_comfy(comfy, first["folder"], first["file"])
    file_server.truncated.clear()
    asked = len(file_server.requested)

    def key(f):
        return f"{Path(f['folder']).name}/{f['file']}"

    changed_url = _sources(file_server)
    spec = changed_url[key(third)]
    changed_url[key(third)] = ModelSpec(spec.name, None, None, None, spec.url + "?rev=2", spec.destination,
                                        spec.bytes, spec.sha256)
    with pytest.raises(AttachedDownloadRefused) as caught:
        apply_attached_model_download(comfy.client, "z-image", "t2i", plan_sha256=plan["plan_sha256"],
                                      sources=changed_url, http_client=_http())
    assert caught.value.code == "plan_changed"

    changed_sha = _sources(file_server)  # the file ComfyUI already lists: its digest is part of the plan too
    spec = changed_sha[key(first)]
    changed_sha[key(first)] = ModelSpec(spec.name, None, None, None, spec.url, spec.destination, spec.bytes,
                                        hashlib.sha256(b"other").hexdigest())
    with pytest.raises(AttachedDownloadRefused) as caught:
        apply_attached_model_download(comfy.client, "z-image", "t2i", plan_sha256=plan["plan_sha256"],
                                      sources=changed_sha, http_client=_http())
    assert caught.value.code == "plan_changed"
    assert file_server.requested[asked:] == [] and not (Path(third["folder"]) / third["file"]).exists()
