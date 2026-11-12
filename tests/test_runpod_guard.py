"""The RunPod network guard (tests/runpod_guard.py) refuses RunPod hosts and allows loopback."""

from __future__ import annotations

import os
import socket
import ssl
import threading

import pytest

from tests import runpod_guard
from tests.runpod_guard import RunPodNetworkBlocked


@pytest.fixture
def no_real_io(monkeypatch):
    """Fail the test if the guard lets a RunPod connection reach the real socket layer."""
    def boom(*a, **kw):
        raise AssertionError("network I/O attempted")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)


@pytest.mark.parametrize("host", [
    "api.runpod.io", "rest.runpod.io", "api.runpod.ai", "hapi.runpod.net",
    "abc123-8000.proxy.runpod.net", "API.RunPod.IO.",
])
def test_create_connection_to_runpod_host_is_refused_without_network_io(host, no_real_io):
    with pytest.raises(RunPodNetworkBlocked, match=r"test_runpod_guard\.py::test_create_connection"):
        socket.create_connection((host, 443), timeout=1)


def test_direct_connection_to_api_runpod_io_is_refused_without_network_io(no_real_io):
    with pytest.raises(RunPodNetworkBlocked, match="api.runpod.io"):
        socket.create_connection(("api.runpod.io", 443))
    with pytest.raises(RunPodNetworkBlocked):
        socket.getaddrinfo("api.runpod.io", 443)
    with pytest.raises(RunPodNetworkBlocked):
        socket.gethostbyname("rest.runpod.io")


def test_http_client_to_runpod_is_refused_before_any_connect(no_real_io):
    import http.client
    conn = http.client.HTTPSConnection("api.runpod.io", timeout=1)
    with pytest.raises(RunPodNetworkBlocked):
        conn.request("GET", "/graphql")


def test_tls_handshake_with_runpod_sni_is_refused_when_tunnelled_via_a_proxy(monkeypatch):
    server = socket.socket()
    server.bind(("127.0.0.1", 0)); server.listen(1)
    port = server.getsockname()[1]
    monkeypatch.setattr(runpod_guard, "_REAL_PROXY_PORTS", frozenset({port}))
    client = socket.create_connection(("127.0.0.1", port))
    try:
        with pytest.raises(RunPodNetworkBlocked, match="SNI"):
            ssl.create_default_context().wrap_socket(client, server_hostname="api.runpod.io")
    finally:
        client.close(); server.close()


def test_http_request_naming_runpod_through_a_proxy_connection_is_refused():
    # a CONNECT / absolute-URI request to a non-loopback-looking peer; socketpair has no inet peer
    a, b = socket.socketpair()
    try:
        # AF_UNIX peers count as local, so exercise the scanner directly
        assert runpod_guard._http_target_hosts(b"CONNECT api.runpod.io:443 HTTP/1.1\r\nHost: api.runpod.io:443\r\n\r\n") == ["api.runpod.io", "api.runpod.io"]
        assert runpod_guard._http_target_hosts(b"GET https://api.runpod.io/v2/pods HTTP/1.1\r\n\r\n") == ["api.runpod.io"]
        assert runpod_guard._http_target_hosts(b"\x16\x03\x01binary") == []
    finally:
        a.close(); b.close()


def test_request_to_runpod_through_a_proxy_port_on_loopback_is_refused(monkeypatch):
    server = socket.socket()
    server.bind(("127.0.0.1", 0)); server.listen(1)
    port = server.getsockname()[1]
    monkeypatch.setattr(runpod_guard, "_REAL_PROXY_PORTS", frozenset({port}))
    try:
        client = socket.create_connection(("127.0.0.1", port))
        with pytest.raises(RunPodNetworkBlocked, match="api.runpod.io"):
            client.sendall(b"CONNECT api.runpod.io:443 HTTP/1.1\r\nHost: api.runpod.io:443\r\n\r\n")
        client.close()
    finally:
        server.close()


def test_loopback_still_works_including_runpod_named_http_to_a_local_fake():
    server = socket.socket()
    server.bind(("127.0.0.1", 0)); server.listen(1)
    port = server.getsockname()[1]
    got = []
    def serve():
        conn, _ = server.accept()
        got.append(conn.recv(1024))
        conn.sendall(b"pong"); conn.close()
    t = threading.Thread(target=serve); t.start()
    try:
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"GET /x HTTP/1.1\r\nHost: api.runpod.io\r\n\r\n")  # fake server; Host header only
        assert client.recv(10) == b"pong"
        client.close()
    finally:
        t.join(5); server.close()
    assert got and b"api.runpod.io" in got[0]


def test_non_runpod_names_are_not_matched():
    for host in ("example.com", "notrunpod.io", "runpod.io.evil.test", "127.0.0.1"):
        assert not runpod_guard.is_runpod_host(host)


def test_credentials_are_absent_from_the_test_environment():
    for name in runpod_guard.CREDENTIAL_ENV_VARS:
        assert name not in os.environ
    import runpod
    assert runpod.api_key is None


def test_only_the_exact_documented_opt_in_disables_the_guard(monkeypatch):
    assert runpod_guard.LIVE_OPT_IN_ENV == "ASTRID_TESTS_ALLOW_LIVE_RUNPOD"
    for value in ("1", "true", "yes", ""):
        monkeypatch.setenv(runpod_guard.LIVE_OPT_IN_ENV, value)
        assert not runpod_guard.live_opt_in()
        with pytest.raises(RunPodNetworkBlocked):
            socket.getaddrinfo("api.runpod.io", 443)
    for name in ("ASTRID_ALLOW_RUNPOD", "RUNPOD_ALLOW", "ASTRID_TESTS_ALLOW_RUNPOD"):
        monkeypatch.setenv(name, runpod_guard.LIVE_OPT_IN_VALUE)
        with pytest.raises(RunPodNetworkBlocked):
            socket.getaddrinfo("api.runpod.io", 443)


def test_a_tests_own_loopback_fake_proxy_is_not_the_real_proxy():
    server = socket.socket()
    server.bind(("127.0.0.1", 0)); server.listen(1)
    client = socket.create_connection(server.getsockname())
    try:
        assert server.getsockname()[1] not in runpod_guard._REAL_PROXY_PORTS
        client.sendall(b"CONNECT pod123-8000.proxy.runpod.net:443 HTTP/1.1\r\n\r\n")  # a fake egress proxy
    finally:
        client.close(); server.close()


@pytest.fixture
def real_proxy_listener(monkeypatch):
    """A loopback listener standing in for the environment's real proxy; records any bytes sent to it."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0)); server.listen(5)
    server.settimeout(0.2)
    port = server.getsockname()[1]
    received: list[bytes] = []
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(0.2)
            try:
                data = conn.recv(4096)
                if data:
                    received.append(data)
            except OSError:
                pass
            conn.close()

    t = threading.Thread(target=serve, daemon=True); t.start()
    url = f"http://127.0.0.1:{port}"
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, url)
    monkeypatch.delenv("NO_PROXY", raising=False); monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(runpod_guard, "_REAL_PROXY_PORTS", frozenset({port}))
    # urllib builds its process-global opener lazily from the proxy variables
    # above. Restore the previous opener so later tests don't keep this closed
    # loopback proxy.
    import urllib.request
    saved_opener = urllib.request._opener
    try:
        yield received
    finally:
        urllib.request.install_opener(saved_opener)
        stop.set(); t.join(2); server.close()


def test_async_httpx_client_to_runpod_via_the_real_proxy_is_refused_with_no_request_sent(real_proxy_listener):
    import asyncio
    import httpx

    async def go():
        async with httpx.AsyncClient(trust_env=True, timeout=3) as client:
            await client.get("https://api.runpod.io/graphql")

    before = len(runpod_guard.blocked_attempts)
    with pytest.raises(Exception):  # httpx/anyio may turn the guard error into a ReadError
        asyncio.run(go())
    assert [h for _, h, _ in runpod_guard.blocked_attempts[before:]] == ["api.runpod.io"]
    assert real_proxy_listener == []


def test_sync_httpx_client_to_runpod_via_the_real_proxy_is_refused_with_no_request_sent(real_proxy_listener):
    import httpx

    before = len(runpod_guard.blocked_attempts)
    with httpx.Client(trust_env=True, timeout=3) as client:
        with pytest.raises(Exception):
            client.get("https://api.runpod.io/v2/pods")
    assert [h for _, h, _ in runpod_guard.blocked_attempts[before:]] == ["api.runpod.io"]
    assert real_proxy_listener == []


def test_requests_and_urllib_to_runpod_via_the_real_proxy_send_nothing(real_proxy_listener):
    import urllib.request
    import requests

    before = len(runpod_guard.blocked_attempts)
    with pytest.raises(Exception):
        requests.get("https://api.runpod.io/graphql", timeout=3)
    with pytest.raises(Exception):
        urllib.request.urlopen("https://api.runpod.io/v2/pods", timeout=3)
    assert len(runpod_guard.blocked_attempts) - before >= 2
    assert real_proxy_listener == []
