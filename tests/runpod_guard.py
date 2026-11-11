"""Test-suite guard: no test may open a network connection to a RunPod host.

Why: the environment's network proxy can inject a real RunPod credential into any
HTTPS request to a RunPod host from any process.  An un-stubbed test would then act
on the real account, authenticated.  "No key in the env" is therefore not a safeguard,
so connections to RunPod hosts are refused at the socket/TLS layer.

Mechanism (all installed once per pytest process by ``install()``):

* ``socket.getaddrinfo`` / ``gethostbyname`` / ``gethostbyname_ex`` /
  ``create_connection`` refuse a RunPod hostname.
* ``ssl.SSLContext.wrap_socket`` refuses a RunPod ``server_hostname`` (SNI).  This also
  catches TLS reached through an HTTP(S) proxy, where the TCP connection only goes to the
  proxy and the RunPod name appears first in the CONNECT request.
* ``socket.socket.send``/``sendall`` refuse an HTTP request whose first line or
  ``Host:`` header names a RunPod host (CONNECT, absolute-URI and plain requests).

Loopback is allowed so tests can run a local fake RunPod server (including a TLS
server whose certificate claims a RunPod name), but a connection to a configured
proxy port is never treated as loopback.

RunPod hosts: ``runpod.io``, ``runpod.ai``, ``runpod.net`` and ``runpod.com`` (and any
subdomain, e.g. ``api.runpod.io``, ``rest.runpod.io``, ``api.runpod.ai``,
``*.proxy.runpod.net``, ``hapi.runpod.net``).

The only switch is the environment variable named in ``LIVE_OPT_IN_ENV`` set to
``LIVE_OPT_IN_VALUE``.  It is meant for a future explicit live lane; no CI job or default
run sets it.  Any other value, or any other name, leaves the guard on.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
from urllib.parse import urlsplit

RUNPOD_DOMAINS = ("runpod.io", "runpod.ai", "runpod.net", "runpod.com")
LIVE_OPT_IN_ENV = "ASTRID_TESTS_ALLOW_LIVE_RUNPOD"
LIVE_OPT_IN_VALUE = "i-accept-that-tests-act-on-the-real-runpod-account"
CREDENTIAL_ENV_VARS = ("RUNPOD_API_KEY", "RUNPOD_AI_API_KEY", "RUNPOD_HTTPS_TOKEN")

_installed = False
_current_test = "<outside a test>"


class RunPodNetworkBlocked(RuntimeError):
    """Raised when a test tries to reach a RunPod host."""


def live_opt_in() -> bool:
    return os.environ.get(LIVE_OPT_IN_ENV) == LIVE_OPT_IN_VALUE


def is_runpod_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    host = host.strip().strip("[]").rstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in RUNPOD_DOMAINS)


def set_current_test(name: str) -> None:
    global _current_test
    _current_test = name


def _refuse(host: str, via: str) -> None:
    raise RunPodNetworkBlocked(
        f"RunPod network access blocked by tests/runpod_guard.py: test {_current_test!r} "
        f"tried to reach {host!r} via {via}. Tests must never contact RunPod (the real "
        "account may be authenticated by the network proxy). Stub the RunPod SDK / "
        "runpod_lifecycle call at its seam, or use a loopback fake server."
    )


def _check_host(host: object, via: str) -> None:
    if not live_opt_in() and is_runpod_host(host):
        _refuse(str(host), via)


def _proxy_ports() -> set[int]:
    ports: set[int] = set()
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name)
        if value:
            try:
                port = urlsplit(value if "://" in value else "http://" + value).port
            except ValueError:
                port = None
            if port:
                ports.add(port)
    return ports


def _peer_is_local_server(sock: socket.socket) -> bool:
    try:
        peer = sock.getpeername()
    except OSError:
        return False
    if isinstance(peer, tuple) and peer:
        try:
            if not ipaddress.ip_address(peer[0].split("%")[0]).is_loopback:
                return False
        except ValueError:
            return False
        return len(peer) < 2 or peer[1] not in _proxy_ports()
    return True  # AF_UNIX


def _http_target_hosts(data: bytes) -> list[str]:
    head = data[:2048]
    if not head[:8].split(b" ")[0].isalpha():
        return []
    hosts: list[str] = []
    lines = head.split(b"\r\n")
    parts = lines[0].decode("latin-1").split(" ")
    if len(parts) >= 2:
        target = parts[1]
        if "://" in target:
            hosts.append(urlsplit(target).hostname or "")
        elif parts[0] == "CONNECT":
            hosts.append(target.rsplit(":", 1)[0])
    for line in lines[1:]:
        if line[:5].lower() == b"host:":
            hosts.append(line[5:].decode("latin-1").strip().rsplit(":", 1)[0])
    return hosts


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_gethostbyname_ex = socket.gethostbyname_ex
    real_create_connection = socket.create_connection
    real_wrap_socket = ssl.SSLContext.wrap_socket
    real_send = socket.socket.send
    real_sendall = socket.socket.sendall

    def getaddrinfo(host, *a, **kw):
        _check_host(host, "socket.getaddrinfo")
        return real_getaddrinfo(host, *a, **kw)

    def gethostbyname(host):
        _check_host(host, "socket.gethostbyname")
        return real_gethostbyname(host)

    def gethostbyname_ex(host):
        _check_host(host, "socket.gethostbyname_ex")
        return real_gethostbyname_ex(host)

    def create_connection(address, *a, **kw):
        _check_host(address[0], "socket.create_connection")
        return real_create_connection(address, *a, **kw)

    def wrap_socket(self, sock, *a, server_hostname=None, **kw):
        if not live_opt_in() and is_runpod_host(server_hostname) and not _peer_is_local_server(sock):
            _refuse(str(server_hostname), "TLS handshake (SNI)")
        return real_wrap_socket(self, sock, *a, server_hostname=server_hostname, **kw)

    def _scan(sock, data):
        if live_opt_in() or not isinstance(data, (bytes, bytearray, memoryview)):
            return
        head = bytes(data[:2048])
        if b"runpod" not in head.lower():
            return
        for host in _http_target_hosts(head):
            if is_runpod_host(host) and not _peer_is_local_server(sock):
                _refuse(host, "an outgoing HTTP request")

    def send(self, data, *a):
        _scan(self, data)
        return real_send(self, data, *a)

    def sendall(self, data, *a):
        _scan(self, data)
        return real_sendall(self, data, *a)

    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.create_connection = create_connection
    ssl.SSLContext.wrap_socket = wrap_socket
    socket.socket.send = send
    socket.socket.sendall = sendall


def scrub_credentials(environ=os.environ) -> None:
    if live_opt_in():
        return
    for name in CREDENTIAL_ENV_VARS:
        environ.pop(name, None)
    try:  # the SDK reads the key once at import time
        import runpod

        runpod.api_key = None
    except Exception:  # noqa: BLE001 - SDK absent or half-imported
        pass
