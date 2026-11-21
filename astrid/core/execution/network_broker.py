"""Tiny observable loopback broker used by provider boundary tests.

This is deliberately a test-sized protocol rather than a general-purpose
proxy.  A child first performs an admission-bound ``ASTRID-BROKER/1``
handshake, then sends ordinary absolute-form HTTP requests through the
loopback listener.  The broker records both events so a successful route is
evidence of a live owner, not a manifest boolean.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import socketserver
import select
import socket
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Mapping
from urllib.parse import urlsplit


@dataclass
class BrokerEvent:
    kind: str
    detail: str = ""


class _BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        broker: "ObservableNetworkBroker" = self.server.broker  # type: ignore[attr-defined]
        first = self.rfile.readline(8192)
        if first.startswith(b"ASTRID-BROKER/1 HELLO "):
            parts = first.decode("utf-8", "replace").strip().split()
            digest = parts[2] if len(parts) > 2 else ""
            nonce = parts[3] if len(parts) > 3 else ""
            auth_token = parts[4] if len(parts) > 4 else ""
            allowed = len(parts) == 5 and broker._admission_allowed(digest, nonce, auth_token)
            broker._record("handshake", f"{digest}:{nonce}", allowed=allowed)
            self.wfile.write(("ASTRID-BROKER/1 OK\n" if allowed else "ASTRID-BROKER/1 REJECT\n").encode("ascii"))
            self.wfile.flush()
            return
        if not first:
            return
        # urllib's proxy request is absolute-form: ``GET http://host/path``.
        line = first.decode("iso-8859-1", "replace").strip()
        headers: dict[str, str] = {}
        while True:
            raw = self.rfile.readline(8192)
            if not raw or raw in {b"\r\n", b"\n"}:
                break
            name, separator, value = raw.decode("iso-8859-1", "replace").partition(":")
            if separator:
                headers[name.lower()] = value.strip()
        if line.startswith("CONNECT "):
            target = line.split(" ", 2)[1] if len(line.split(" ", 2)) > 1 else ""
            # CONNECT is an explicit raw TCP route.  Keep that distinction in
            # evidence instead of disguising SSH as an HTTPS request.
            route = f"tcp://{target}"
            allowed = broker._route_allowed(route)
            broker._record("route", route, allowed=allowed)
            if not allowed:
                self._response(HTTPStatus.FORBIDDEN, b"broker route was not admitted")
                return
            try:
                host, raw_port = target.rsplit(":", 1)
                upstream = socket.create_connection((host.strip("[]"), int(raw_port)), timeout=10)
            except (OSError, ValueError):
                self._response(HTTPStatus.BAD_GATEWAY, b"broker could not connect upstream")
                return
            try:
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
                self._tunnel(upstream)
            finally:
                upstream.close()
            return
        if not line.startswith(("GET ", "POST ", "HEAD ")):
            broker.events.append(BrokerEvent("rejected", line[:120]))
            self._response(HTTPStatus.BAD_REQUEST, b"broker requires absolute-form HTTP")
            return
        target = line.split(" ", 2)[1]
        allowed = broker._route_allowed(target)
        broker._record("route", target, allowed=allowed)
        if not allowed:
            self._response(HTTPStatus.FORBIDDEN, b"broker route was not admitted")
            return
        if broker.response_body is not None:
            self._response(HTTPStatus.OK, broker.response_body)
            return
        try:
            self._forward_http(line, headers)
        except (OSError, ValueError):
            self._response(HTTPStatus.BAD_GATEWAY, b"broker could not connect upstream")

    def _forward_http(self, line: str, headers: Mapping[str, str]) -> None:
        """Forward an absolute-form HTTP request when no fixture body exists."""
        parts = line.split(" ", 2)
        if len(parts) != 3:
            raise ValueError("malformed proxy request")
        method, target, _version = parts
        parsed = urlsplit(target)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("absolute HTTP target required")
        port = parsed.port or 80
        upstream = socket.create_connection((parsed.hostname, port), timeout=10)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            request = f"{method} {path} HTTP/1.1\r\n"
            for name, value in headers.items():
                if name not in {"proxy-connection", "connection", "host"}:
                    request += f"{name}: {value}\r\n"
            request += f"Host: {parsed.hostname}:{port}\r\nConnection: close\r\n\r\n"
            upstream.sendall(request.encode("iso-8859-1"))
            self._tunnel(upstream)
        finally:
            upstream.close()

    def _tunnel(self, upstream: socket.socket) -> None:
        """Relay a CONNECT stream until either side closes it."""
        client = self.connection
        broker = self.server.broker
        deadline = time.monotonic() + min(600.0, broker.tunnel_idle_seconds)
        while not broker._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            readable, _, _ = select.select((client, upstream), (), (), min(0.25, remaining))
            if not readable:
                continue
            for source in readable:
                try:
                    data = source.recv(65536)
                    if data:
                        (upstream if source is client else client).sendall(data)
                except OSError:
                    return
                if not data:
                    return
                deadline = time.monotonic() + min(600.0, broker.tunnel_idle_seconds)

    def _response(self, status: HTTPStatus, body: bytes) -> None:
        self.wfile.write(
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode("ascii") + body
        )
        self.wfile.flush()


class _BrokerServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], broker: "ObservableNetworkBroker") -> None:
        self.broker = broker
        super().__init__(address, _BrokerHandler)


@dataclass
class ObservableNetworkBroker:
    """Live loopback broker with admission-fenced route evidence.

    A broker created without an admission remains permissive for compatibility
    with the small legacy fixture. Production/provider journeys must call
    :meth:`register_admission` before starting traffic; then both the handshake
    and every absolute-form route are checked against the exact admission.
    """

    response_body: bytes | None = b"broker-response"
    events: list[BrokerEvent] = field(default_factory=list)
    expected_admission_digest: str = ""
    expected_nonce: str = ""
    allowed_routes: tuple[str, ...] = ()
    evidence_path: Path | None = None
    evidence_key: str = ""
    auth_token: str = ""
    defer_evidence: bool = False
    tunnel_idle_seconds: float = 15.0
    _stopping: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _strict: bool = field(default=False, init=False, repr=False)
    _admission: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _server: _BrokerServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def register_admission(
        self,
        admission: Mapping[str, Any],
        *,
        allowed_routes: list[str] | tuple[str, ...] = (),
        evidence_path: str | Path | None = None,
        evidence_key: str = "",
        auth_token: str = "",
    ) -> "ObservableNetworkBroker":
        """Pre-register the host's immutable admission and route allowlist."""
        self._admission = dict(admission)
        self.expected_admission_digest = hashlib.sha256(
            json.dumps(self._admission, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.expected_nonce = str(self._admission.get("network_nonce") or self._admission.get("nonce") or "")
        self.allowed_routes = tuple(str(item) for item in allowed_routes)
        self.evidence_path = Path(evidence_path) if evidence_path is not None else None
        self.evidence_key = str(evidence_key)
        self.auth_token = str(auth_token)
        self._strict = True
        return self

    def _record(self, kind: str, detail: str, *, allowed: bool = True) -> None:
        self.events.append(BrokerEvent(kind, f"{detail}|allowed={str(allowed).lower()}"))
        if not self.defer_evidence:
            self._write_evidence()

    def _admission_allowed(self, digest: str, nonce: str, auth_token: str) -> bool:
        if not self._strict:
            return bool(digest)
        if not all(isinstance(value, str) and value.isascii() and value and len(value) <= 256
                   and not any(c.isspace() for c in value) for value in (digest, nonce, auth_token)):
            return False
        return bool(
            self.expected_admission_digest
            and hmac.compare_digest(digest, self.expected_admission_digest)
            and self.expected_nonce
            and hmac.compare_digest(nonce, self.expected_nonce)
            and self.auth_token
            and hmac.compare_digest(auth_token, self.auth_token)
        )

    def _route_allowed(self, target: str) -> bool:
        if not self._strict:
            return True
        try:
            parsed = urlsplit(target)
            parsed_port = parsed.port
        except ValueError:
            return False
        if (parsed.scheme not in {"http", "https", "tcp"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return False
        default_port = 443 if parsed.scheme == "https" else 80
        if parsed.scheme == "tcp":
            default_port = None
        normalized = f"{parsed.scheme}://{parsed.hostname.lower()}:{parsed_port or default_port}"
        if parsed.scheme != "tcp":
            normalized += parsed.path or "/"
        for route in self.allowed_routes:
            candidate = str(route).strip()
            if candidate == target or candidate == normalized:
                return True
            # A declared host:port destination is allowed for any path, but
            # never a different host or port.
            try:
                has_scheme = "://" in candidate
                declared = urlsplit(candidate if has_scheme else f"https://{candidate}")
                # A bare host declaration intentionally covers its resolved
                # HTTP/HTTPS port range; an explicit URL keeps its port fence.
                declared_port = declared.port if has_scheme or ":" in candidate.rsplit("/", 1)[-1] else None
            except ValueError:
                continue
            # CONNECT exposes only an authority, not the encrypted protocol.
            # Admit its TCP transport for an HTTPS declaration at the same
            # host and port, while recording the actual tcp route as evidence.
            https_tunnel = parsed.scheme == "tcp" and has_scheme and declared.scheme == "https"
            if has_scheme and declared.scheme != parsed.scheme and not https_tunnel:
                continue
            if https_tunnel and declared_port is None:
                declared_port = 443
            actual_port = parsed_port or default_port
            if declared.hostname and declared.hostname.lower() == parsed.hostname.lower() and (declared_port is None or declared_port == actual_port):
                return True
        return False

    def _write_evidence(self) -> None:
        if not self.evidence_path or not self.evidence_key:
            return
        unsigned = {
            "schema_version": 1,
            "admission": dict(self._admission),
            "events": [{"kind": event.kind, "detail": event.detail} for event in self.events],
        }
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        payload = {
            **unsigned,
            "signature_algorithm": "hmac-sha256",
            "signature": hmac.new(self.evidence_key.encode(), canonical, hashlib.sha256).hexdigest(),
        }
        self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def start(self) -> "ObservableNetworkBroker":
        if self._server is not None:
            return self
        self._stopping.clear()
        self._server = _BrokerServer(("127.0.0.1", 0), self)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def endpoint(self) -> str:
        if self._server is None:
            raise RuntimeError("broker is not started")
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._stopping.set()
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    def finalize_evidence(self) -> None:
        """Rewrite signed evidence from broker-owned events after child exit."""
        self._write_evidence()

    def evidence(self) -> list[dict[str, Any]]:
        return [{"kind": event.kind, "detail": event.detail} for event in self.events]


__all__ = ["BrokerEvent", "ObservableNetworkBroker"]
