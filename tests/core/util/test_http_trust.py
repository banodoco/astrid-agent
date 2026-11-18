"""OS trust policy for outbound HTTPS: factory branches and a real localhost TLS server."""

from __future__ import annotations

import datetime
import http.server
import ssl
import threading
import urllib.error
import urllib.request
from ipaddress import ip_address

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from astrid.core.util import http as http_util
from astrid.core.util.http import (
    os_trust_ssl_context,
    trusted_async_httpx_client,
    trusted_httpx_client,
)

_TRUSTSTORE_MODULE = "astrid.installer._tls_truststore._api"


@pytest.fixture(autouse=True)
def _no_user_trust_override(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_windows_and_macos_use_the_os_store_context(platform):
    context = os_trust_ssl_context(platform)

    assert context.__class__.__module__ == _TRUSTSTORE_MODULE
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize("platform", ["linux", "freebsd", "cygwin"])
def test_other_platforms_use_the_stdlib_default_context(platform):
    context = os_trust_ssl_context(platform)

    assert context.__class__ is ssl.SSLContext
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_user_cert_store_choice_is_respected_on_windows(monkeypatch, variable):
    monkeypatch.setenv(variable, "/tmp/unused-ca-bundle")

    context = os_trust_ssl_context("win32")

    assert context.__class__ is ssl.SSLContext


def test_empty_user_cert_store_variable_does_not_override(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "")

    context = os_trust_ssl_context("win32")

    assert context.__class__.__module__ == _TRUSTSTORE_MODULE


def test_factory_does_not_patch_the_global_ssl_module():
    before = (ssl.SSLContext, ssl.create_default_context, ssl._create_default_https_context)

    os_trust_ssl_context("win32")
    os_trust_ssl_context("linux")

    assert (ssl.SSLContext, ssl.create_default_context, ssl._create_default_https_context) == before


def test_trusted_httpx_client_always_verifies_with_the_factory_context(monkeypatch):
    sentinel = ssl.create_default_context()
    recorded = []

    class _RecordingClient:
        def __init__(self, **kwargs):
            recorded.append(kwargs)

    monkeypatch.setattr(http_util, "os_trust_ssl_context", lambda: sentinel)
    monkeypatch.setattr(httpx, "Client", _RecordingClient)
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)

    trusted_httpx_client(timeout=7, verify=False)
    trusted_async_httpx_client(follow_redirects=False)

    assert recorded[0] == {"timeout": 7, "verify": sentinel}
    assert recorded[1] == {"follow_redirects": False, "verify": sentinel}


# ---------------------------------------------------------------------------
# Real TLS: a private CA signs a localhost certificate; only trust decides.
# ---------------------------------------------------------------------------


def _write_private_ca_and_localhost_cert(directory):
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    later = now + datetime.timedelta(days=3650)

    def _key():
        return ec.generate_private_key(ec.SECP256R1())

    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Private Root")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(later)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = _key()
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(later)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = directory / "ca.pem"
    cert_path = directory / "server.pem"
    key_path = directory / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"verified"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture
def private_https_server(tmp_path, monkeypatch):
    ca_path, cert_path, key_path = _write_private_ca_and_localhost_cert(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Never route the loopback test through a sandbox or developer proxy.
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "localhost,127.0.0.1")
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield f"https://localhost:{server.server_address[1]}/", ca_path
    finally:
        server.shutdown()
        server.server_close()


def _cert_failure_in_chain(error: BaseException) -> bool:
    seen = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if isinstance(current, urllib.error.URLError) and isinstance(current.reason, BaseException):
            pending.append(current.reason)
        pending.extend(link for link in (current.__cause__, current.__context__) if link is not None)
    return False


def test_default_httpx_client_rejects_a_private_root(private_https_server):
    url, _ca_path = private_https_server

    with httpx.Client(trust_env=False, timeout=5) as client, pytest.raises(httpx.ConnectError) as raised:
        client.get(url)

    assert _cert_failure_in_chain(raised.value)


def test_factory_trusts_a_private_root_through_the_user_store_choice(private_https_server, monkeypatch):
    url, ca_path = private_https_server
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))

    with trusted_httpx_client(trust_env=False, timeout=5) as client:
        response = client.get(url)

    assert response.status_code == 200
    assert response.text == "verified"


def test_urllib_with_factory_context_trusts_the_private_root(private_https_server, monkeypatch):
    url, ca_path = private_https_server
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    context = os_trust_ssl_context()

    with urllib.request.urlopen(url, timeout=5, context=context) as response:
        assert response.read() == b"verified"


def test_urllib_default_rejects_the_private_root(private_https_server):
    url, _ca_path = private_https_server

    with pytest.raises(urllib.error.URLError) as raised:
        urllib.request.urlopen(url, timeout=5)

    assert _cert_failure_in_chain(raised.value)


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_certifi_supplements_the_os_store_from_the_first_connection(
    private_https_server, monkeypatch, platform,
):
    import certifi

    url, ca_path = private_https_server
    monkeypatch.setattr(certifi, "where", lambda: str(ca_path))
    context = os_trust_ssl_context(platform)

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    with urllib.request.urlopen(url, timeout=5, context=context) as response:
        assert response.read() == b"verified"


def test_factory_keeps_the_default_os_roots_when_certifi_is_added(monkeypatch):
    original = ssl.create_default_context()
    roots = set(original.get_ca_certs(binary_form=True))
    monkeypatch.setattr(ssl, "create_default_context", lambda: original)

    context = os_trust_ssl_context("linux")

    assert context is original
    assert roots <= set(context.get_ca_certs(binary_form=True))


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_dependency_free_bootstrap_keeps_verifying_without_certifi(monkeypatch, platform):
    import sys

    monkeypatch.setitem(sys.modules, "certifi", None)

    context = os_trust_ssl_context(platform)

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_factory_still_rejects_an_untrusted_private_root(private_https_server):
    url, _ca_path = private_https_server

    with trusted_httpx_client(trust_env=False, timeout=5) as client, pytest.raises(httpx.ConnectError) as raised:
        client.get(url)

    assert _cert_failure_in_chain(raised.value)
