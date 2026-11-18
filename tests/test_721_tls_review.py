"""Regression proof for the Windows readiness and TLS diagnostics review."""

from __future__ import annotations

import io
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from astrid.core.contracts import errors
from astrid.core.util import http as http_util


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8188",
        "http://localhost:8188",
        "http://[::1]:8188",
    ],
)
def test_http_readiness_does_not_construct_a_tls_context(monkeypatch, endpoint):
    from astrid.packs.vibecomfy.actions.run.run import _engine_for

    def forbidden_context():
        raise AssertionError("plain HTTP must not initialize native TLS trust")

    calls = []

    def open_request(url, *, timeout, context=None):
        calls.append((url, timeout, context))
        return io.BytesIO(b'{"ExampleNode": {}}')

    monkeypatch.setattr(http_util, "os_trust_ssl_context", forbidden_context)
    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    result = _engine_for({"engine": "comfyui"}).probe(endpoint, timeout=7)

    assert result == {"ready": True, "endpoint": endpoint, "nodes": 1}
    assert calls == [(endpoint + "/object_info", 7, None)]


def test_cold_truststore_import_under_native_descendant_policy(tmp_path):
    # A fresh process prevents an earlier test from warming the native import.
    # Model Python 3.11's Windows platform.system() subprocess on every OS;
    # the real pack descendant guard, rather than a permissive fake, refuses it.
    program = r"""
import platform
import ssl
import subprocess
import sys
from astrid.core.execution import network_policy
from astrid.core.util.http import os_trust_ssl_context

assert "astrid.installer._tls_truststore._api" not in sys.modules
events = []
network_policy._record = lambda kind, **fields: events.append((kind, fields))
network_policy._validated_descendant_owner = lambda: False
originals = {}
network_policy._patch_native_descendants(originals)

def forbidden_platform_probe():
    return subprocess.check_output(["cmd", "/c", "ver"]).decode()

platform.system = forbidden_platform_probe
context = os_trust_ssl_context("win32")
assert context.verify_mode == ssl.CERT_REQUIRED
assert context.check_hostname is True
assert context.__class__.__module__ == "astrid.installer._tls_truststore._api"
assert not events, events
try:
    subprocess.Popen([sys.executable, "-c", "pass"])
except network_policy.NetworkPolicyError:
    pass
else:
    raise AssertionError("the descendant guard was disabled")
assert events[-1][0] == "native_descendant" and events[-1][1]["allowed"] is False
print("cold truststore verified; native descendants still refused")
"""
    environment = dict(os.environ)
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        environment.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-I", "-c", program],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cold truststore verified; native descendants still refused" in result.stdout


def _certificate_failure(message="self-signed certificate in certificate chain", code=19):
    error = ssl.SSLCertVerificationError(1, message)
    error.verify_code = code
    error.verify_message = message
    return urllib.error.URLError(error)


@pytest.mark.parametrize(
    "variables",
    [
        ("SSL_CERT_FILE",),
        ("SSL_CERT_DIR",),
        ("SSL_CERT_FILE", "SSL_CERT_DIR"),
    ],
)
def test_explicit_store_error_names_the_selected_variables(monkeypatch, variables):
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    for name in variables:
        monkeypatch.setenv(name, "/private/selected-certificate-store")
    error = errors.coerce_astrid_error(_certificate_failure())
    message = error.cause + " " + error.fix + " " + error.recovery_command

    assert error.code == errors.TLS_VERIFICATION_CODE and error.degraded is False
    assert all(name in message for name in variables)
    assert "system certificate store" not in message
    assert "/private/selected-certificate-store" not in message


@pytest.mark.parametrize(
    "code,message",
    [
        (62, "Hostname mismatch, certificate is not valid for 'files.example.test'."),
        (64, "IP address mismatch, certificate is not valid for '127.0.0.1'."),
    ],
)
def test_hostname_mismatch_has_specific_guidance(monkeypatch, code, message):
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    error = errors.coerce_astrid_error(_certificate_failure(message, code))
    guidance = (error.cause + " " + error.fix + " " + error.recovery_command).lower()

    assert error.code == errors.TLS_VERIFICATION_CODE and error.degraded is False
    assert "hostname" in guidance
    assert "antivirus" not in guidance and "https scanning" not in guidance


@pytest.mark.parametrize("opener", ["urlopen", "handlers"])
def test_installer_urllib_failure_retains_only_the_host(monkeypatch, opener):
    from astrid.installer import tls_fallback

    url = "https://user:secret@files.example.test/private?token=secret#secret"
    original = _certificate_failure()

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fail)
    request = urllib.request.Request(url)
    with pytest.raises(urllib.error.URLError) as raised:
        if opener == "urlopen":
            tls_fallback.urlopen_with_tls_fallback(request, timeout=1)
        else:
            tls_fallback.open_with_tls_fallback(request, timeout=1)

    assert raised.value is original
    error = errors.coerce_astrid_error(raised.value)
    rendered = str(error.to_envelope())
    assert "files.example.test" in error.cause
    assert "secret" not in rendered and "/private" not in rendered and "user:" not in rendered


@pytest.mark.parametrize(
    "seam",
    [
        "article",
        "workflow",
        "embedding",
        "shared_get",
        "shared_post",
        "openai",
        "catalog",
        "http_client",
        "comfy_https",
        "readiness",
        "acquire",
    ],
)
def test_urllib_host_survives_the_real_transport_boundaries(monkeypatch, tmp_path, seam):
    from astrid.core.execution.executor import banodoco_catalog
    from astrid.core.generation.backends import comfy_target, openai
    from astrid.core.hivemind.executors import _common, embedding_contract
    from astrid.core.hivemind.executors.ingest_article import run as article
    from astrid.core.hivemind.executors.ingest_workflow import run as workflow
    from astrid.core.model_setup import acquire
    from astrid.packs.vibecomfy.actions.run.run import _engine_for

    url = "https://user:secret@files.example.test/private?token=secret#secret"
    request = urllib.request.Request(url)
    original = _certificate_failure()

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fail)
    monkeypatch.setattr(http_util, "urlopen", fail)
    monkeypatch.setattr(openai, "urlopen", fail)
    calls = {
        "article": lambda: article.fetch_html(url),
        "workflow": lambda: workflow.load_workflow_from_url(url),
        "embedding": lambda: embedding_contract._stdlib_transport(url, {}, {}),
        "shared_get": lambda: _common._http_get(url, {}),
        "shared_post": lambda: _common._http_post(url, {}, b"{}"),
        "openai": lambda: openai._request_json(request, 1, expected_count=1),
        "catalog": lambda: banodoco_catalog._fetch_catalog_payload(
            SimpleNamespace(catalog_url=url, timeout_seconds=1)
        ),
        "http_client": lambda: http_util._real_transport(request),
        "comfy_https": lambda: comfy_target.open_comfy_http(request, timeout=1),
        "acquire": lambda: acquire._download(
            SimpleNamespace(artifact_id="fixture"), url, tmp_path / "part", 0, urlopen=None
        ),
    }
    if seam == "readiness":
        result = _engine_for({"engine": "comfyui"}).probe(url)
        assert result["ready"] is False
        failure = result["exception"]
    else:
        with pytest.raises(Exception) as raised:
            calls[seam]()
        failure = raised.value
    error = errors.coerce_astrid_error(failure)
    rendered = str(error.to_envelope())
    assert error.code == errors.TLS_VERIFICATION_CODE
    assert "files.example.test" in error.cause
    assert "secret" not in rendered and "/private" not in rendered and "user:" not in rendered
