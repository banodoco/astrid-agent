"""Verify the additional Agent-owned network openings found by the TLS audit."""

from __future__ import annotations

import contextlib
import hashlib
import io
import sys
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

from astrid import omp_agent
from astrid.core.generation import model_downloader
from astrid.core.generation.backends import comfy_target
from astrid.core.generation.engine_contracts import ModelSpec
from astrid.core.model_setup import acquire
from astrid.core.util import http as http_util


def test_openrouter_key_check_uses_the_shared_context(monkeypatch, capsys):
    context = object()
    seen = []
    monkeypatch.setattr(omp_agent, "os_trust_ssl_context", lambda: context)
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * 32))
    monkeypatch.setenv("ASTRID_OPENROUTER_API_BASE", "https://openrouter.example.test")

    def open_request(request, *, timeout, context):
        seen.append((request.full_url, timeout, context))
        raise urllib.error.URLError(OSError("test stops before credential import"))

    monkeypatch.setattr(omp_agent.urllib.request, "urlopen", open_request)

    assert omp_agent._run_key_command(["set", "openrouter"]) == 4
    assert seen == [("https://openrouter.example.test/api/v1/auth/key", 10, context)]
    assert "provider_unreachable" in capsys.readouterr().out


def test_managed_comfy_https_keeps_shared_trust_and_redirect_refusal(monkeypatch):
    context = object()
    handlers = []
    response = object()
    request = urllib.request.Request("https://comfy.example.test/object_info")
    monkeypatch.setattr(comfy_target, "os_trust_ssl_context", lambda: context)

    def build_opener(*values):
        handlers.extend(values)

        def open_request(actual_request, *, timeout):
            assert actual_request is request
            assert timeout == 7
            return response

        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(comfy_target.urllib_request, "build_opener", build_opener)

    assert comfy_target.open_comfy_http(request, timeout=7) is response
    assert comfy_target._NoRedirectHandler in handlers
    tls_handlers = [value for value in handlers if isinstance(value, urllib.request.HTTPSHandler)]
    assert len(tls_handlers) == 1
    assert tls_handlers[0]._context is context


def test_loopback_comfy_keeps_proxy_bypass_without_constructing_tls(monkeypatch):
    handlers = []

    def no_tls():
        raise AssertionError("plain loopback HTTP must not construct a TLS context")

    monkeypatch.setattr(comfy_target, "os_trust_ssl_context", no_tls)
    monkeypatch.setattr(
        comfy_target.urllib_request, "build_opener",
        lambda *values: handlers.extend(values) or SimpleNamespace(open=lambda *_args, **_kwargs: None),
    )

    comfy_target.open_comfy_http(urllib.request.Request("http://127.0.0.1:8188/object_info"), timeout=5)

    assert comfy_target._NoRedirectHandler in handlers
    proxies = [value for value in handlers if isinstance(value, urllib.request.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}


def test_artifact_acquisition_default_opener_uses_shared_trust(monkeypatch, tmp_path):
    context = object()
    seen = []
    payload = b"verified artifact bytes"
    monkeypatch.setattr(acquire, "os_trust_ssl_context", lambda: context)

    def open_request(request, *, context):
        seen.append((request.full_url, context))
        return io.BytesIO(payload)

    monkeypatch.setattr(acquire.urllib.request, "urlopen", open_request)
    part = tmp_path / "artifact.part"

    size = acquire._download(
        SimpleNamespace(artifact_id="fixture"), "https://artifacts.example.test/model",
        part, 0, urlopen=None,
    )

    assert size == len(payload) and part.read_bytes() == payload
    assert seen == [("https://artifacts.example.test/model", context)]


def test_artifact_acquisition_preserves_an_injected_context_manager(tmp_path):
    payload = b"caller-owned transport"
    closed = []

    @contextlib.contextmanager
    def open_request(request):
        assert request.full_url == "https://artifacts.example.test/model"
        with io.BytesIO(payload) as response:
            yield response
        closed.append(True)

    part = tmp_path / "artifact.part"

    size = acquire._download(
        SimpleNamespace(artifact_id="fixture"), "https://artifacts.example.test/model",
        part, 0, urlopen=open_request,
    )

    assert size == len(payload) and part.read_bytes() == payload
    assert closed == [True]


def test_default_model_range_download_uses_one_shared_context_and_checks_hash(monkeypatch, tmp_path):
    context = object()
    created = []
    requests = []
    payload = b"verified model bytes"

    def make_context():
        created.append(context)
        return context

    def open_request(request, *, timeout, context):
        start, end = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
        assert 0 < timeout <= 60
        requests.append((start, end, context))
        response = io.BytesIO(payload[start:end + 1])
        response.status = 206
        response.headers = {"Content-Range": f"bytes {start}-{end}/{len(payload)}"}
        return response

    monkeypatch.setattr(http_util, "os_trust_ssl_context", make_context)
    monkeypatch.setattr(model_downloader.urllib.request, "urlopen", open_request)
    digest = hashlib.sha256(payload).hexdigest()
    model = ModelSpec(
        name="fixture", repository=None, revision=None, path=None,
        url="https://models.example.test/model.bin", destination="model.bin",
        bytes=len(payload), sha256=digest,
    )
    root = tmp_path / "models"

    result = model_downloader._download_model(
        model, models_root=str(root), deadline=time.monotonic() + 10, range_bytes=8,
    )

    assert created == [context]
    assert requests == [(0, 7, context), (8, 15, context), (16, 19, context)]
    assert result.verified is True and result.sha256 == digest
    assert (root / "model.bin").read_bytes() == payload


def test_comfy_readiness_probe_uses_the_shared_context(monkeypatch):
    from astrid.packs.vibecomfy.actions.run.run import _engine_for

    context = object()
    seen = []
    monkeypatch.setattr(http_util, "os_trust_ssl_context", lambda: context)

    def open_request(url, *, timeout, context):
        seen.append((url, timeout, context))
        return io.BytesIO(b'{"ExampleNode":{}}')

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    engine = _engine_for({"engine": "comfyui"})

    result = engine.probe("https://comfy.example.test", timeout=7)

    assert result["ready"] is True and result["nodes"] == 1
    assert seen == [("https://comfy.example.test/object_info", 7, context)]
