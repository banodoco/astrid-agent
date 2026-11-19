"""Focused behavioral coverage for the issue 721 urllib trust policy."""
from __future__ import annotations

import json
import ssl

from astrid.core.generation.backends import openai
from astrid.core.hivemind.executors import _common, embedding_contract
from astrid.core.hivemind.executors.ingest_article import run as ingest_article
from astrid.core.hivemind.executors.ingest_workflow import run as ingest_workflow
from astrid.packs.training.actions.search_loras import run as search_loras


class _Response:
    def __init__(self, body: bytes):
        self.body = body
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.body


def _install_transport(monkeypatch, module, body: bytes):
    marker = object()
    seen = []
    monkeypatch.setattr(module, "os_trust_ssl_context", lambda: marker, raising=False)

    def fake_urlopen(request, *, timeout, context=None):
        seen.append((request, timeout, context))
        return _Response(body)

    if hasattr(module, "urlopen"):
        monkeypatch.setattr(module, "urlopen", fake_urlopen)
    else:
        monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    return marker, seen


def _assert_policy(marker, seen):
    assert seen and all(context is marker for _, _, context in seen)


def test_openai_request_uses_os_trust_context(monkeypatch):
    marker = object()
    seen = []
    monkeypatch.setattr(openai, "os_trust_ssl_context", lambda: marker, raising=False)
    monkeypatch.setattr(openai, "urlopen", lambda request, *, timeout, context=None: (seen.append(context) or _Response(b'{"ok":true}')))
    assert openai._request_json(openai.Request("https://api.openai.com/test"), 10, expected_count=1) == {"ok": True}
    assert seen == [marker]


def test_embedding_transport_uses_os_trust_context(monkeypatch):
    marker, seen = _install_transport(monkeypatch, embedding_contract, b'{"data":[{"embedding":[0.1]}]}')
    assert embedding_contract._stdlib_transport("https://api.openai.com/v1/embeddings", {}, {})["data"]
    _assert_policy(marker, seen)


def test_article_and_workflow_fetches_use_os_trust_context(monkeypatch):
    marker, seen = _install_transport(monkeypatch, ingest_article, b"<html>article</html>")
    assert "article" in ingest_article.fetch_html("https://example.test/article")
    _assert_policy(marker, seen)

    marker, seen = _install_transport(monkeypatch, ingest_workflow, b'{"name":"workflow"}')
    assert ingest_workflow.load_workflow_from_url("https://example.test/workflow") == {"name": "workflow"}
    _assert_policy(marker, seen)


def test_search_loras_and_shared_hivemind_http_use_os_trust_context(monkeypatch):
    marker, seen = _install_transport(monkeypatch, search_loras, b"[]")
    assert search_loras._fetch_models(
        base_model=None, query="test", limit=1, sort="downloads", direction="desc",
        token=None, timeout=5,
    ) == []
    _assert_policy(marker, seen)

    marker, seen = _install_transport(monkeypatch, _common, b'{"ok":true}')
    assert _common._http_get("https://example.test", {}) == {"ok": True}
    _assert_policy(marker, seen)

    marker, seen = _install_transport(monkeypatch, _common, b'{"ok":true}')
    assert _common._http_post("https://example.test", {}, b"{}") == {"ok": True}
    _assert_policy(marker, seen)


def test_ingest_cli_preserves_structured_tls_error_and_legacy_network_shape(monkeypatch, tmp_path):
    import urllib.error

    cases = (
        (ingest_article, ["--url", "https://articles.example.test", "--out"], "Failed to fetch"),
        (ingest_workflow, ["--url", "https://workflows.example.test", "--out"], "failed to read workflow"),
    )
    for module, argv, legacy_prefix in cases:
        tls_path = tmp_path / f"{module.__name__.split('.')[-2]}-tls.json"
        tls_error = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
        monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, _error=tls_error, **kwargs: (_ for _ in ()).throw(_error))
        assert module.main([*argv, str(tls_path)]) == 1
        tls_output = json.loads(tls_path.read_text())
        assert tls_output["error"]["code"] == "tls_verification_failed"

        network_path = tmp_path / f"{module.__name__.split('.')[-2]}-network.json"
        network_error = urllib.error.URLError(OSError("connection refused"))
        monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, _error=network_error, **kwargs: (_ for _ in ()).throw(_error))
        assert module.main([*argv, str(network_path)]) == 1
        network_output = json.loads(network_path.read_text())
        assert isinstance(network_output["error"], str)
        assert network_output["error"].startswith(legacy_prefix)
