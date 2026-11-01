"""Compatibility aliases use catalog transport without changing their bundles."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("ASTRID_INTERNAL_INVOCATION", "1")
from astrid.core.generation.backends.fal import FalBackend
from astrid.packs.fal.actions.h3_video import run as h3
from astrid.packs.fal.actions.fal_foley import run as foley
from astrid.packs.seedance_local.actions.reference_video import run as seedance


class Client:
    def register_secret(self, value):
        assert value == "fake-key"

    def get_bytes(self, *args, **kwargs):
        raise AssertionError("unexpected network download")


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    monkeypatch.setattr(FalBackend, "_resolve_api_key", lambda self: "fake-key")
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected credential lookup")
    monkeypatch.setattr(h3.CredentialsScope, "get_local", forbidden)
    monkeypatch.setattr(seedance.CredentialsScope, "get", forbidden)
    monkeypatch.setattr(foley, "load_provider_key", lambda *a, **kw: "fake-key")


def args_for(module, tmp_path, *, dry=False, seed=None):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Use @Image1 then @Image2 with @Video1 and ambient audio.")
    paths = []
    for name in ("one.png", "two.webp", "motion.mp4", "sound.wav"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        paths.append(path)
    argv = ["--out", str(tmp_path / "out"), "--prompt-file", str(prompt),
            "--image-ref", str(paths[0]), "--image-ref", str(paths[1]),
            "--video-ref", str(paths[2]), "--timeout-seconds", "173"]
    if module is h3:
        argv += ["--mode", "reference-to-video", "--audio-ref", str(paths[3])]
    if seed is not None:
        argv += ["--seed", str(seed)]
    if dry:
        argv += ["--dry-run"]
    return module._parser().parse_args(argv)


def hooks(result):
    captured = {"uploads": [], "downloads": []}
    def upload(client, path, key):
        captured["uploads"].append(path.name)
        assert path.is_file() and key == "fake-key"
        return "https://fake.invalid/" + path.name
    def submit(client, endpoint, payload, key, **kwargs):
        captured.update(endpoint=endpoint, payload=payload, kwargs=kwargs)
        return result
    def download(client, url, destination, timeout):
        captured["downloads"].append((url, destination, timeout))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"generated")
    return captured, dict(client=Client(), api_key="fake-key", uploader=upload,
                          submitter=submit, downloader=download)


@pytest.mark.parametrize("video", [{"url": "https://fake.invalid/result.mp4"}, "https://fake.invalid/result.mp4"])
def test_h3_mixed_reference_order_timeout_and_result_bundle(tmp_path, video):
    args = args_for(h3, tmp_path)
    captured, injected = hooks({"request_id": "request-7", "video": video})
    code, manifest = h3.execute(args, **injected)
    assert (code, manifest["status"], manifest["kind"]) == (0, "completed", "fal.h3_video")
    assert captured["uploads"] == ["image1.png", "image2.webp", "video1.mp4", "audio1.wav"]
    payload = captured["payload"]
    for kind, names in (("image", ["image1.png", "image2.webp"]), ("video", ["video1.mp4"]), ("audio", ["audio1.wav"])):
        assert payload[f"reference_{kind}_urls"] == ["https://fake.invalid/" + name for name in names]
    assert captured["kwargs"] == {"max_wait_sec": 173}
    assert captured["downloads"][0][2] == 173
    assert manifest["outputs"][0]["path"] == "outputs/minimax-h3-reference-to-video.mp4"
    assert manifest["provider_extension"]["request_id"] == "request-7"
    assert (args.out / "h3_video_receipt.json").is_file()
    assert "fake.invalid" not in (args.out / "request.json").read_text()


@pytest.mark.parametrize("video", [{"url": "https://fake.invalid/result.mp4"}, "https://fake.invalid/result.mp4"])
@pytest.mark.parametrize("seed", [None, 43])
def test_seedance_preserves_order_duration_seed_and_bundle(tmp_path, monkeypatch, video, seed):
    monkeypatch.setattr(seedance, "_probe_video", lambda p: {"duration_seconds": 8, "width": 1280, "height": 704, "codec": "h264"})
    args = args_for(seedance, tmp_path, seed=seed)
    captured, injected = hooks({"request_id": "request-8", "video": video})
    code, manifest = seedance.execute(args, **injected)
    assert (code, manifest["status"]) == (0, "completed")
    assert captured["uploads"] == ["image1.png", "image2.webp", "video1.mp4"]
    assert captured["payload"]["duration"] == "15"
    assert captured["payload"]["image_urls"] == ["https://fake.invalid/image1.png", "https://fake.invalid/image2.webp"]
    assert captured["payload"]["video_urls"] == ["https://fake.invalid/video1.mp4"]
    assert captured["payload"].get("seed") == seed
    assert ("seed" in captured["payload"]) is (seed is not None)
    assert captured["kwargs"] == {"max_wait_sec": 173}
    assert manifest["outputs"][0]["path"] == "outputs/seedance-2-reference-media.mp4"
    assert "fake.invalid" not in (args.out / "request.json").read_text()
    assert (args.out / "manifest.json").is_file()


@pytest.mark.parametrize("module", [h3, seedance])
@pytest.mark.parametrize("result", [{}, {"video": "http://fake.invalid/unsafe.mp4"}])
def test_video_alias_rejects_missing_or_non_https_without_network(tmp_path, monkeypatch, module, result):
    monkeypatch.setattr(seedance, "_probe_video", lambda p: {"duration_seconds": 8, "width": 1280, "height": 704, "codec": "h264"})
    args = args_for(module, tmp_path)
    captured, injected = hooks(result)
    # Exercise the real shared downloader's HTTPS gate with a network-forbidden client.
    injected["downloader"] = module._download_video
    code, manifest = module.execute(args, **injected)
    assert code == 1 and manifest["status"] == "failed"
    assert not (args.out / "outputs").exists()
    assert "video.url" in manifest["error"] or "HTTPS" in manifest["error"]


@pytest.mark.parametrize("module", [h3, seedance])
def test_dry_run_does_not_touch_credentials_or_provider(tmp_path, monkeypatch, module):
    monkeypatch.setattr(seedance, "_probe_video", lambda p: {"duration_seconds": 8, "width": 1280, "height": 704, "codec": "h264"})
    def forbidden(*args, **kwargs):
        raise AssertionError("dry run reached provider")
    monkeypatch.setattr(FalBackend, "submit", forbidden)
    code, manifest = module.execute(args_for(module, tmp_path, dry=True))
    assert code == 0 and manifest["status"] == "draft" and manifest["outputs"] == []


@pytest.mark.parametrize("key", ["audio", "output_audio", "video"])
def test_foley_response_shapes_keep_download_timeout(tmp_path, monkeypatch, key):
    calls = []
    def download(client, url, destination, timeout, **kwargs):
        calls.append((url, destination, timeout, kwargs))
        destination.write_bytes(b"audio")
        return destination, b"audio"
    monkeypatch.setattr(FalBackend, "download", staticmethod(download))
    destination = tmp_path / "audio.wav"
    saved = foley._save_audio(Client(), {key: {"url": "https://fake.invalid/a.wav", "content_type": "audio/wav"}}, destination)
    assert saved["path"] == str(destination) and saved["bytes"] == 5
    assert calls == [("https://fake.invalid/a.wav", destination, 300, {"fallback_suffix": ".wav", "infer_suffix": False})]


def test_foley_custom_wait_and_dry_run(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"clip")
    captured = {}
    downloads = []
    registered_keys = []
    class FoleyClient(Client):
        def register_secret(self, value):
            registered_keys.append(value)

        def get_bytes(self, url, *, timeout, max_bytes):
            downloads.append((url, timeout, max_bytes))
            return b"audio"
    fake_client = FoleyClient()
    monkeypatch.setattr(foley, "default_client", lambda: fake_client)
    def submit(client, endpoint, payload, key, **kwargs):
        assert client is fake_client and key == "fake-key"
        assert endpoint == "fal-ai/hunyuan-video-foley"
        captured.update(payload=payload, kwargs=kwargs)
        return {"request_id": "foley-47", "audio": {"url": "https://fake.invalid/a.wav"}}
    monkeypatch.setattr(foley, "fal_submit_and_poll", submit)
    argv = ["--clip", str(clip), "--prompt", "rain", "--out", str(tmp_path / "audio.wav"), "--max-wait-sec", "47"]
    assert foley.main(argv) == 0
    assert captured["kwargs"] == {"max_wait_sec": 47}
    assert captured["payload"] == {"text_prompt": "rain", "video_url": "data:video/mp4;base64,Y2xpcA=="}
    assert registered_keys == ["fake-key"]
    assert downloads == [("https://fake.invalid/a.wav", 300, 512 * 1024 * 1024)]
    assert (tmp_path / "audio.wav").read_bytes() == b"audio"
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["kind"] == "fal.fal_foley"
    assert manifest["outputs"][0]["path"] == "audio.wav"
    assert manifest["outputs"][0]["bytes"] == 5
    assert manifest["provider_extension"]["request_id"] == "foley-47"
    sidecar = json.loads((tmp_path / "audio.wav.fal.json").read_text())
    assert sidecar["saved"]["bytes"] == 5
    captured.clear()
    def forbidden(*args, **kwargs):
        raise AssertionError("dry run reached credentials or provider client")
    monkeypatch.setattr(foley, "load_provider_key", forbidden)
    monkeypatch.setattr(foley, "default_client", forbidden)
    assert foley.main(argv + ["--dry-run"]) == 0
    assert captured == {}
    assert len(downloads) == 1


@pytest.mark.parametrize("capability,extra", [("fal.h3_video", {"mode": "reference-to-video"}), ("seedance_local.reference_video", {})])
@pytest.mark.parametrize("reference", ["/caller/local.png", [{"digest": "sha256:" + "b" * 64}]])
def test_managed_alias_references_refuse_before_task_creation(capability, extra, reference):
    from astrid.sdk import invocation
    admissions = []
    client = SimpleNamespace(tasks=SimpleNamespace(create=lambda **kw: admissions.append(kw)))
    result = invocation.invoke_result(capability, kind="executor", project="demo", client=client,
        inputs={"prompt_file": {"digest": "sha256:" + "a" * 64}, "image_ref": reference, **extra})
    assert not result.ok
    assert result.error["sdk_category"] == "validation"
    assert result.kernel_task_id is None and result.kernel_run_id is None
    assert admissions == []


def test_h3_overlong_exact_prompt_refuses_before_credentials_or_upload(tmp_path, monkeypatch):
    args = args_for(h3, tmp_path)
    args.prompt_file.write_text("x" * 2001)
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid prompt reached provider")
    monkeypatch.setattr(FalBackend, "submit", forbidden)
    code, manifest = h3.execute(args)
    assert code == 1 and manifest["status"] == "failed"
    assert "2000" in manifest["error"]
    assert (args.out / "inputs" / "prompt.txt").read_text().strip() == "x" * 2001


def test_seedance_missing_reference_label_refuses_before_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(seedance, "_probe_video", lambda p: {"duration_seconds": 8, "width": 1280, "height": 704, "codec": "h264"})
    args = args_for(seedance, tmp_path)
    args.prompt_file.write_text("Use @Image1 and @Video1.")
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid reference prompt reached provider")
    monkeypatch.setattr(FalBackend, "submit", forbidden)
    with pytest.raises(ValueError, match="@Image2"):
        seedance.execute(args)
