"""Fake-provider coverage for both catalog speech repair stages."""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("ASTRID_INTERNAL_INVOCATION", "1")
from astrid.core.generation.backends import fal
from astrid.packs.media.actions.speech_repair_lavasr import run


class Client:
    def register_secret(self, value):
        assert value == "fake-key"


@pytest.mark.parametrize("name,preferred,alternate", [("lava-sr", "audio", "audio_file"), ("deepfilternet3", "audio_file", "audio")])
@pytest.mark.parametrize("both", [False, True])
def test_catalog_speech_stage_response_preference_and_sanitized_receipt(tmp_path, monkeypatch, name, preferred, alternate, both):
    source = tmp_path / "input.wav"
    source.write_bytes(b"speech")
    destination = tmp_path / "repaired.wav"
    receipt = tmp_path / "response.json"
    calls = {}
    monkeypatch.setattr(fal, "default_client", Client)
    monkeypatch.setattr(fal.FalBackend, "_resolve_api_key", lambda self: "fake-key")
    monkeypatch.setattr(fal, "fal_storage_upload", lambda c, p, k: "https://fake.invalid/input.wav")
    result = {alternate: {"url": "https://fake.invalid/alternate.wav?token=secret"}, "request_id": "request-9"}
    if both:
        result[preferred] = {"url": "https://fake.invalid/preferred.wav?token=secret"}
    def submit(client, endpoint, payload, key, **kwargs):
        calls.update(endpoint=endpoint, payload=payload)
        return result
    def download(client, url, path, timeout, **kwargs):
        calls.update(url=url, timeout=timeout)
        path.write_bytes(b"repaired")
        return path, b"repaired"
    monkeypatch.setattr(fal, "fal_submit_and_poll", submit)
    monkeypatch.setattr(fal.FalBackend, "download", staticmethod(download))
    function = run._run_lavasr if name == "lava-sr" else run._run_deepfilternet3
    function(source, destination, receipt, None)
    assert calls["endpoint"] == "fal-ai/" + name
    assert calls["payload"]["audio_url"] == "https://fake.invalid/input.wav"
    if name == "lava-sr":
        assert calls["payload"]["audio_format"] == "wav" and calls["payload"]["bitrate"] == "192k"
    assert calls["url"].split("/")[-1].startswith("preferred" if both else "alternate")
    assert calls["timeout"] == 120 and destination.read_bytes() == b"repaired"
    recorded = receipt.read_text()
    assert "fake.invalid" not in recorded and "token=secret" not in recorded
    assert json.loads(recorded)["request_id"] == "request-9"


@pytest.mark.parametrize("name", ["lava-sr", "deepfilternet3"])
def test_missing_speech_url_refuses_before_download(tmp_path, monkeypatch, name):
    source = tmp_path / "input.wav"
    source.write_bytes(b"speech")
    monkeypatch.setattr(fal, "default_client", Client)
    monkeypatch.setattr(fal.FalBackend, "_resolve_api_key", lambda self: "fake-key")
    monkeypatch.setattr(fal, "fal_storage_upload", lambda *a: "https://fake.invalid/input.wav")
    monkeypatch.setattr(fal, "fal_submit_and_poll", lambda *a, **kw: {})
    def forbidden(*args, **kwargs):
        raise AssertionError("missing URL reached downloader")
    monkeypatch.setattr(fal.FalBackend, "download", staticmethod(forbidden))
    with pytest.raises(run.AstridError, match="did not include an audio URL"):
        run._run_catalog_audio(name, source, tmp_path / "out.wav", tmp_path / "response.json", None)


@pytest.mark.parametrize("name", ["lava-sr", "deepfilternet3"])
def test_non_https_speech_output_refuses_before_network(tmp_path, monkeypatch, name):
    from astrid.core.contracts.errors import AstridValueError
    source = tmp_path / "input.wav"
    source.write_bytes(b"speech")
    monkeypatch.setattr(fal, "default_client", Client)
    monkeypatch.setattr(fal.FalBackend, "_resolve_api_key", lambda self: "fake-key")
    monkeypatch.setattr(fal, "fal_storage_upload", lambda *a: "https://fake.invalid/input.wav")
    monkeypatch.setattr(fal, "fal_submit_and_poll", lambda *a, **kw: {"audio": {"url": "http://fake.invalid/unsafe.wav"}})
    destination = tmp_path / "out.wav"
    with pytest.raises(AstridValueError, match="HTTPS"):
        run._run_catalog_audio(name, source, destination, tmp_path / "response.json", None)
    assert not destination.exists()
