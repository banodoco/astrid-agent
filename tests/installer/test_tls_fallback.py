"""Installer connections use one verified trust policy, without trust retries."""

from __future__ import annotations

import hashlib
import io
import shutil
import ssl
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

from astrid.core.util import http as http_util
from astrid.installer import tools


@pytest.fixture
def policy_context(monkeypatch):
    context = ssl.create_default_context()
    monkeypatch.setattr(http_util, "os_trust_ssl_context", lambda: context)
    return context


@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "urlerror"])
def test_certificate_failure_stops_after_one_verified_attempt(tmp_path, monkeypatch, wrapped, policy_context):
    calls = []
    content = b"verified archive bytes"
    original_context = ssl.SSLContext
    original_default = ssl._create_default_https_context
    error = ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")
    if wrapped:
        error = urllib.error.URLError(error)

    def urlopen(request, *, timeout, **kwargs):
        calls.append((request, timeout, kwargs))
        context = kwargs["context"]
        assert context is policy_context
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        raise error

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    target = tmp_path / "archive"
    with pytest.raises(type(error)) as caught:
        tools.download("https://example.invalid/archive", target,
                       sha256=hashlib.sha256(content).hexdigest())

    assert caught.value is error
    assert len(calls) == 1
    assert calls[0][2] == {"context": policy_context}
    assert calls[0][0].get_header("User-agent") == f"astrid-installer/{tools.ASTRID_VERSION}"
    assert calls[0][1] == 120
    assert not target.exists()
    assert ssl.SSLContext is original_context
    assert ssl._create_default_https_context is original_default


def test_repeated_certificate_failure_is_never_retried(tmp_path, monkeypatch, policy_context):
    calls = []
    error = urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))

    def urlopen(request, *, timeout, **kwargs):
        calls.append(kwargs)
        assert kwargs == {"context": policy_context}
        assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["context"].check_hostname is True
        raise error

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    target = tmp_path / "archive"
    with pytest.raises(urllib.error.URLError) as caught:
        tools.download("https://example.invalid/archive", target)
    assert caught.value is error
    assert len(calls) == 1
    assert not target.exists()


@pytest.mark.parametrize("error", [
    urllib.error.URLError("CERTIFICATE_VERIFY_FAILED"),  # Text alone is not evidence.
    urllib.error.URLError(ConnectionRefusedError("refused")),
    urllib.error.HTTPError("https://example.invalid/archive", 503, "unavailable", {}, None),
    ssl.SSLError(1, "TLS protocol error"),
    TimeoutError("timed out"),
    OSError("network down"),
])
def test_other_errors_are_not_retried(tmp_path, monkeypatch, error, policy_context):
    calls = []

    def urlopen(request, *, timeout, **kwargs):
        calls.append(kwargs)
        raise error

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    with pytest.raises(type(error)) as caught:
        tools.download("https://example.invalid/archive", tmp_path / "archive")
    assert caught.value is error
    assert calls == [{"context": policy_context}]


def test_success_uses_one_verified_attempt_without_changing_ssl(tmp_path, monkeypatch, policy_context):
    calls = []
    original_context = ssl.SSLContext
    original_default = ssl._create_default_https_context

    def urlopen(request, *, timeout, **kwargs):
        calls.append(kwargs)
        return io.BytesIO(b"archive")

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    target = tmp_path / "archive"
    tools.download("https://example.invalid/archive", target)
    assert target.read_bytes() == b"archive"
    assert calls == [{"context": policy_context}]
    assert ssl.SSLContext is original_context
    assert ssl._create_default_https_context is original_default


def test_shared_trust_still_rejects_a_checksum_mismatch(tmp_path, monkeypatch, policy_context):
    calls = []

    def urlopen(request, *, timeout, **kwargs):
        calls.append(kwargs)
        assert kwargs["context"] is policy_context
        assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["context"].check_hostname is True
        return io.BytesIO(b"tampered archive")

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    with pytest.raises(ValueError, match="Download checksum mismatch"):
        tools.download("https://example.invalid/archive", tmp_path / "archive", sha256="0" * 64)
    assert len(calls) == 1


def test_shared_policy_works_in_dependency_free_bootstrap(tmp_path, monkeypatch):
    # Exercise the Windows/macOS OS-store policy independently of a host CA
    # override; explicit user overrides have separate factory coverage.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    # Use the real wheel configuration to prove the verifier and its license
    # also reach an installed bootstrap; no dependency resolution is needed.
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(root / "pyproject.toml", source / "pyproject.toml")
    files = [Path(name) for name in (
        "astrid/__init__.py", "astrid/installer/_tls_policy.py", "astrid/version.py", "astrid/installer/__init__.py",
        "astrid/installer/tls_fallback.py",
        "astrid/core/__init__.py",
        "astrid/core/ids.py",
    )]
    for package in ("util", "foundation", "contracts", "audit"):
        files.extend(path.relative_to(root) for path in
                     (root / "astrid/core" / package).glob("*.py"))
    files.extend(path.relative_to(root) for path in
                 (root / "astrid/installer/_tls_truststore").glob("*.py"))
    for path in files:
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / path, target)
    built = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(tmp_path / "dist")],
        cwd=source, capture_output=True, text=True, check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheel_path, = (tmp_path / "dist").glob("*.whl")
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel_path) as wheel:
        for path in files:
            assert wheel.read(path.as_posix()) == (root / path).read_bytes()
        license_source = wheel.read("astrid/installer/_tls_truststore/__init__.py")
        assert b"Copyright (c) 2022 Seth Michael Larson" in license_source
        assert b"The above copyright notice and this permission notice" in license_source
        wheel.extractall(installed)

    # -I -S removes the test venv's installed packages, including any ambient
    # truststore/certifi. This exercises the bundled verifier, without network.
    script = r'''
import importlib.util, io, ssl, sys, urllib.error, urllib.request
sys.path.insert(0, sys.argv[1])
assert importlib.util.find_spec("truststore") is None
assert importlib.util.find_spec("certifi") is None
from astrid.installer.tls_fallback import urlopen_with_tls_fallback
from astrid.core.util import http as http_util
calls = []
original_context = ssl.SSLContext
original_default = ssl._create_default_https_context
policy_context = http_util.os_trust_ssl_context("win32")
http_util.os_trust_ssl_context = lambda: policy_context
def urlopen(request, *, timeout, **kwargs):
    calls.append(kwargs)
    context = kwargs["context"]
    assert context is policy_context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.__class__.__module__ == "astrid.installer._tls_truststore._api"
    return io.BytesIO(b"verified")
urllib.request.urlopen = urlopen
with urlopen_with_tls_fallback(urllib.request.Request("https://example.invalid"), timeout=120) as response:
    assert response.read() == b"verified"
assert len(calls) == 1
assert ssl.SSLContext is original_context
assert ssl._create_default_https_context is original_default
'''
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(installed)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_open_with_tls_fallback_keeps_handlers_and_uses_shared_policy_once(monkeypatch, policy_context):
    from astrid.installer import tls_fallback

    seen = []

    class _Opener:
        def open(self, request, *, timeout):
            assert timeout == 120
            return io.BytesIO(b"verified")

    def build_opener(*handlers):
        seen.append(handlers)
        return _Opener()

    monkeypatch.setattr(tls_fallback.urllib.request, "build_opener", build_opener)
    request = tls_fallback.urllib.request.Request("https://example.invalid")

    with tls_fallback.open_with_tls_fallback(request, timeout=120, handlers=("redirect",)) as response:
        assert response.read() == b"verified"

    assert seen[0][0] == "redirect"
    native = seen[0][-1]
    assert isinstance(native, tls_fallback.urllib.request.HTTPSHandler)
    assert native._context is policy_context
    assert native._context.verify_mode == ssl.CERT_REQUIRED
    assert native._context.check_hostname is True
    assert len(seen) == 1


def test_open_with_tls_fallback_does_not_retry_other_errors(monkeypatch):
    from astrid.installer import tls_fallback

    attempts = []

    class _Opener:
        def open(self, request, *, timeout):
            attempts.append(request)
            raise urllib.error.URLError(OSError("connection refused"))

    monkeypatch.setattr(tls_fallback.urllib.request, "build_opener", lambda *_h: _Opener())

    with pytest.raises(urllib.error.URLError):
        tls_fallback.open_with_tls_fallback(
            tls_fallback.urllib.request.Request("https://example.invalid"), timeout=120,
        )
    assert len(attempts) == 1
