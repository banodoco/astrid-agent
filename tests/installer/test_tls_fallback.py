"""Installer download attempts stay bounded and retain certificate verification."""

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

from astrid.installer import tools


@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "urlerror"])
def test_certificate_failure_retries_once_with_verified_context(tmp_path, monkeypatch, wrapped):
    calls = []
    content = b"verified archive bytes"
    original_context = ssl.SSLContext
    original_default = ssl._create_default_https_context
    error = ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")
    if wrapped:
        error = urllib.error.URLError(error)

    def urlopen(request, *, timeout, **kwargs):
        calls.append((request, timeout, kwargs))
        if len(calls) == 1:
            raise error
        context = kwargs["context"]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        return io.BytesIO(content)

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    target = tmp_path / "archive"
    tools.download("https://example.invalid/archive", target,
                   sha256=hashlib.sha256(content).hexdigest())

    # Budget: exactly the default attempt and one verified retry, with the
    # same URL, User-Agent and timeout; no subprocess or dependency download.
    assert len(calls) == 2
    assert calls[0][2] == {}
    assert calls[0][:2] == calls[1][:2]
    assert calls[0][0].get_header("User-agent") == f"astrid-installer/{tools.ASTRID_VERSION}"
    assert calls[0][1] == 120
    assert target.read_bytes() == content
    assert ssl.SSLContext is original_context
    assert ssl._create_default_https_context is original_default


def test_repeated_certificate_failure_stops_after_two_attempts(tmp_path, monkeypatch):
    calls = []
    error = urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))

    def urlopen(request, *, timeout, **kwargs):
        calls.append(kwargs)
        if kwargs:
            assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
            assert kwargs["context"].check_hostname is True
        raise error

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    target = tmp_path / "archive"
    with pytest.raises(urllib.error.URLError) as caught:
        tools.download("https://example.invalid/archive", target)
    assert caught.value is error
    assert len(calls) == 2
    assert not target.exists()


@pytest.mark.parametrize("error", [
    urllib.error.URLError("CERTIFICATE_VERIFY_FAILED"),  # Text alone is not evidence.
    urllib.error.URLError(ConnectionRefusedError("refused")),
    urllib.error.HTTPError("https://example.invalid/archive", 503, "unavailable", {}, None),
    ssl.SSLError(1, "TLS protocol error"),
    TimeoutError("timed out"),
    OSError("network down"),
])
def test_other_errors_are_not_retried(tmp_path, monkeypatch, error):
    calls = []

    def urlopen(request, *, timeout, **kwargs):
        calls.append(kwargs)
        raise error

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    with pytest.raises(type(error)) as caught:
        tools.download("https://example.invalid/archive", tmp_path / "archive")
    assert caught.value is error
    assert calls == [{}]


def test_default_success_uses_one_attempt_without_changing_ssl(tmp_path, monkeypatch):
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
    assert calls == [{}]
    assert ssl.SSLContext is original_context
    assert ssl._create_default_https_context is original_default


def test_retry_still_rejects_a_checksum_mismatch(tmp_path, monkeypatch):
    calls = []

    def urlopen(request, *, timeout, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))
        assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["context"].check_hostname is True
        return io.BytesIO(b"tampered archive")

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    with pytest.raises(ValueError, match="Download checksum mismatch"):
        tools.download("https://example.invalid/archive", tmp_path / "archive", sha256="0" * 64)
    assert len(calls) == 2


def test_retry_works_in_dependency_free_bootstrap(tmp_path):
    # Use the real wheel configuration to prove the verifier and its license
    # also reach an installed bootstrap; no dependency resolution is needed.
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(root / "pyproject.toml", source / "pyproject.toml")
    files = [Path(name) for name in (
        "astrid/__init__.py", "astrid/version.py", "astrid/installer/__init__.py",
        "astrid/installer/tls_fallback.py",
    )]
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
calls = []
original_context = ssl.SSLContext
def urlopen(request, *, timeout, **kwargs):
    calls.append(kwargs)
    if len(calls) == 1:
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))
    context = kwargs["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.__class__.__module__ == "astrid.installer._tls_truststore._api"
    return io.BytesIO(b"verified")
urllib.request.urlopen = urlopen
with urlopen_with_tls_fallback(urllib.request.Request("https://example.invalid"), timeout=120) as response:
    assert response.read() == b"verified"
assert len(calls) == 2
assert ssl.SSLContext is original_context
'''
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(installed)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_open_with_tls_fallback_keeps_handlers_and_retries_once_natively(monkeypatch):
    from astrid.installer import tls_fallback

    seen = []

    class _Opener:
        def __init__(self, fail):
            self.fail = fail

        def open(self, request, *, timeout):
            assert timeout == 120
            if self.fail:
                raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))
            return io.BytesIO(b"verified")

    def build_opener(*handlers):
        seen.append(handlers)
        return _Opener(fail=len(seen) == 1)

    monkeypatch.setattr(tls_fallback.urllib.request, "build_opener", build_opener)
    request = tls_fallback.urllib.request.Request("https://example.invalid")

    with tls_fallback.open_with_tls_fallback(request, timeout=120, handlers=("redirect",)) as response:
        assert response.read() == b"verified"

    assert seen[0] == ("redirect",)
    assert seen[1][0] == "redirect"
    native = seen[1][-1]
    assert isinstance(native, tls_fallback.urllib.request.HTTPSHandler)
    assert native._context.__class__.__module__ == "astrid.installer._tls_truststore._api"
    assert len(seen) == 2


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
