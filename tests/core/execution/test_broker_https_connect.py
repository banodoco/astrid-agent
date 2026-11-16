"""HTTPS proxy CONNECT routes preserve the declared host and port fence."""
from __future__ import annotations

import socket
import threading

from astrid.core.execution.network_broker import ObservableNetworkBroker


def _echo_upstream() -> tuple[socket.socket, threading.Thread, int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                data = connection.recv(128)
                if data:
                    connection.sendall(data)
        finally:
            listener.close()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    return listener, worker, port


def _connect(endpoint: str, authority: str) -> tuple[socket.socket, bytes]:
    _, port = endpoint.rsplit(":", 1)
    client = socket.create_connection(("127.0.0.1", int(port)), timeout=2)
    client.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
    response = bytearray()
    while b"\r\n\r\n" not in response:
        response.extend(client.recv(1024))
    return client, bytes(response)


def _broker(route: str) -> ObservableNetworkBroker:
    return ObservableNetworkBroker(response_body=None, tunnel_idle_seconds=2).register_admission(
        {"network_nonce": "nonce", "admission_digest": "0" * 64},
        allowed_routes=[route],
        auth_token="token",
    ).start()


def test_https_declaration_allows_real_connect_tunnel_and_fences_destination():
    listener, upstream, port = _echo_upstream()
    broker = _broker(f"https://localhost:{port}")
    allowed = None
    denied_host = None
    denied_port = None
    try:
        allowed, response = _connect(broker.endpoint, f"localhost:{port}")
        assert response.startswith(b"HTTP/1.1 200 Connection Established")
        allowed.sendall(b"tls-like payload")
        assert allowed.recv(128) == b"tls-like payload"
        allowed.close()
        allowed = None

        denied_host, host_response = _connect(broker.endpoint, f"example.invalid:{port}")
        assert host_response.startswith(b"HTTP/1.1 403 Forbidden")
        denied_host.close()
        denied_host = None

        denied_port, port_response = _connect(broker.endpoint, f"localhost:{port + 1}")
        assert port_response.startswith(b"HTTP/1.1 403 Forbidden")
    finally:
        for client in (allowed, denied_host, denied_port):
            if client is not None:
                client.close()
        broker.stop()
        listener.close()
        upstream.join(timeout=2)


def test_raw_tcp_declaration_still_allows_connect():
    listener, upstream, port = _echo_upstream()
    broker = _broker(f"tcp://localhost:{port}")
    client = None
    try:
        client, response = _connect(broker.endpoint, f"localhost:{port}")
        assert response.startswith(b"HTTP/1.1 200 Connection Established")
        client.sendall(b"raw tcp payload")
        assert client.recv(128) == b"raw tcp payload"
    finally:
        if client is not None:
            client.close()
        broker.stop()
        listener.close()
        upstream.join(timeout=2)


def test_bare_host_declaration_retains_scheme_agnostic_tcp_routes():
    broker = _broker("localhost")
    try:
        assert broker._route_allowed("tcp://localhost:8443")
    finally:
        broker.stop()
