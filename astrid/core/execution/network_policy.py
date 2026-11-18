"""Small, observable network policy hook for generic pack children.

This is intentionally an application-level hook, not a sandbox.  It is useful
for the Python provider fixtures and for recording what the child attempted;
an operating-system firewall remains the enforcement point for arbitrary
native binaries.  The hook rejects undeclared DNS/TCP/UDP destinations and
records only non-sensitive endpoint metadata.
"""

from __future__ import annotations

import atexit
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlsplit


_LOCK = threading.Lock()
_INSTALLED = False
_DNS_NAMES: dict[str, set[str]] = {}
_EVENTS: list[dict[str, Any]] = []
_POLICY: dict[str, Any] = {}
_EVIDENCE: Path | None = None
_EVIDENCE_KEY = ""
_ADMISSION: dict[str, Any] = {}
_RESIDENT_JOB = False


class NetworkPolicyError(RuntimeError):
    """A child attempted a network operation outside its admitted policy."""


def _policy_bool(policy: Mapping[str, Any], name: str, default: bool = False) -> bool:
    value = policy.get(name)
    if isinstance(value, Mapping):
        value = value.get("allow", value.get("enabled", default))
    return bool(default if value is None else value)


def _destinations(policy: Mapping[str, Any]) -> tuple[str, ...]:
    values = policy.get("allowed_destinations", policy.get("destinations", ()))
    if isinstance(values, str):
        values = (values,)
    return tuple(str(value).strip().lower() for value in (values or ()) if str(value).strip())


def _protocol_allowed(protocol: str) -> bool:
    if _RESIDENT_JOB:
        from .resident_sessions import ResidentProtocolError, current_job

        try:
            current_job()
        except ResidentProtocolError:
            return False  # Background or completed contexts cannot borrow the next job's routes.
    protocols = _POLICY.get("allowed_protocols", _POLICY.get("protocols"))
    if protocols is None:
        return _policy_bool(_POLICY, protocol, _policy_bool(_POLICY, "network", False))
    if isinstance(protocols, str):
        protocols = (protocols,)
    return protocol.lower() in {str(item).lower() for item in protocols}


def _endpoint_parts(address: Any) -> tuple[str, int | None]:
    if isinstance(address, tuple) and address:
        host = str(address[0])
        port = int(address[1]) if len(address) > 1 and str(address[1]).isdigit() else None
        return host.lower(), port
    raw = str(address)
    if "://" in raw:
        parsed = urlsplit(raw)
        return (parsed.hostname or "").lower(), parsed.port
    if raw.startswith("[") and "]" in raw:
        host, _, port = raw[1:].partition("]")
        return host.lower(), int(port[1:]) if port.startswith(":") and port[1:].isdigit() else None
    host, separator, port = raw.rpartition(":")
    return (host if separator and port.isdigit() else raw).lower(), int(port) if separator and port.isdigit() else None


def _allowed_destination(host: str, port: int | None) -> bool:
    host = host.strip("[]").lower().rstrip(".")
    allowed = _destinations(_POLICY)
    if not allowed:
        return False
    aliases = {host}
    aliases.update(_DNS_NAMES.get(host, set()))
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        aliases.add(str(ip.ipv4_mapped))
    for candidate in allowed:
        candidate_host, candidate_port = _endpoint_parts(candidate)
        if candidate_host in {"*", host} or candidate_host in aliases:
            if candidate_port is None or port is None or candidate_port == port:
                return True
        try:
            if ip is not None and ipaddress.ip_address(candidate_host) == ip:
                if candidate_port is None or port is None or candidate_port == port:
                    return True
        except ValueError:
            continue
    return False


def _record(kind: str, *, host: str = "", port: int | None = None, allowed: bool, detail: str = "") -> None:
    event = {"kind": kind, "host": host, "port": port, "allowed": bool(allowed), "pid": os.getpid(), "time_ns": time.time_ns()}
    if detail:
        event["detail"] = detail
    with _LOCK:
        _EVENTS.append(event)


def _check(kind: str, address: Any) -> tuple[str, int | None]:
    host, port = _endpoint_parts(address)
    allowed = _protocol_allowed(kind) and _allowed_destination(host, port)
    _record(kind, host=host, port=port, allowed=allowed)
    if not allowed:
        raise NetworkPolicyError(f"network policy denied {kind} destination {host}:{port or ''}")
    return host, port


def _write_evidence() -> None:
    if _EVIDENCE is None:
        return
    protocols = _POLICY.get("allowed_protocols", _POLICY.get("protocols", ()))
    if isinstance(protocols, str):
        protocols = (protocols,)
    payload = {"schema_version": 1, "pid": os.getpid(), "events": list(_EVENTS), "limitations": [
        "application_hook_only; native children outside Python are not OS-firewall isolated",
    ], "admission": dict(_ADMISSION), "policy": {
        "allowed_destinations": list(_destinations(_POLICY)),
        "allowed_protocols": list(protocols or ()),
        "redirects": _policy_bool(_POLICY, "redirects", _policy_bool(_POLICY, "allow_redirects", False)),
        "proxy": bool(_POLICY.get("proxy")),
    }}
    broker_path = os.environ.get("ASTRID_NETWORK_BROKER_EVIDENCE", "")
    if broker_path and "admission_digest" not in _ADMISSION:
        try:
            broker_value = json.loads(Path(broker_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            broker_value = None
        if isinstance(broker_value, Mapping):
            # The child may copy the broker record into its diagnostic
            # envelope, but must not validate or sign it: only the parent has
            # the broker-only signing secret.  The host re-reads and verifies
            # the broker file independently after the child exits.
            payload["broker_evidence"] = broker_value
    # Child observations are diagnostic only.  The child must never receive
    # the signing key for settlement evidence, and an unsigned child file is
    # never accepted as authority by the host.  Host/broker evidence is
    # finalized and signed by the parent after the child exits.
    _EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    _EVIDENCE.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _kernel_socketpair() -> bool:
    import _socket

    return hasattr(_socket, "socketpair")


def _patch_socket(originals: Mapping[tuple[Any, str], Any]) -> None:
    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any):
        value = originals[(socket, "getaddrinfo")](host, port, *args, **kwargs)
        name = str(host).lower().rstrip(".")
        ips = {str(item[4][0]).lower() for item in value if item and len(item) > 4 and item[4]}
        _DNS_NAMES.setdefault(name, set()).update(ips)
        for ip in ips:
            _DNS_NAMES.setdefault(ip, set()).add(name)
        allowed = _protocol_allowed("dns") and _allowed_destination(name, None)
        _record("dns", host=name, port=int(port) if str(port).isdigit() else None, allowed=allowed)
        if not allowed:
            raise NetworkPolicyError(f"network policy denied dns destination {name}")
        return value

    # socket.socketpair() is a kernel call on POSIX. Where the kernel has none
    # (Windows), the standard library builds the pair from a loopback listener and
    # connects to it in Python; asyncio's event loop needs that pair, and the connect
    # guard would refuse the ephemeral port. Rather than relax the guard for a window
    # of time, the pair is built here: the only unchecked connect is the one to the
    # listener this call itself just opened, and the two ends are verified to be
    # connected to each other before they are returned.
    def socketpair(*args: Any, **kwargs: Any):
        if _kernel_socketpair():
            # Untouched: the standard library's own defaults apply (AF_UNIX on POSIX).
            return originals[(socket, "socketpair")](*args, **kwargs)
        return _loopback_pair(*args, **kwargs)

    def _loopback_pair(family: socket.AddressFamily = socket.AF_INET, type: int = socket.SOCK_STREAM, proto: int = 0):
        host = {socket.AF_INET: "127.0.0.1", socket.AF_INET6: "::1"}.get(family)
        if host is None or type != socket.SOCK_STREAM or proto != 0:
            raise ValueError("Only AF_INET and AF_INET6 stream socket pairs are supported")
        listener = socket.socket(family, type, proto)
        try:
            listener.bind((host, 0))
            listener.listen()
            target = listener.getsockname()[:2]
            client = socket.socket(family, type, proto)
            try:
                client.setblocking(False)
                try:
                    originals[(socket.socket, "connect")](client, target)
                except (BlockingIOError, InterruptedError):
                    pass
                client.setblocking(True)
                server, _ = listener.accept()
            except BaseException:
                client.close()
                raise
        finally:
            listener.close()
        if server.getsockname() != client.getpeername() or client.getsockname() != server.getpeername():
            server.close()
            client.close()
            raise ConnectionError("Unexpected peer connection")
        _record("tcp", host=str(target[0]), port=int(target[1]), allowed=True, detail="local-socketpair")
        return server, client

    def connect(self: socket.socket, address: Any):
        _check("tcp" if self.type & socket.SOCK_STREAM else "udp", address)
        return originals[(socket.socket, "connect")](self, address)

    def connect_ex(self: socket.socket, address: Any):
        _check("tcp" if self.type & socket.SOCK_STREAM else "udp", address)
        return originals[(socket.socket, "connect_ex")](self, address)

    def sendto(self: socket.socket, data: Any, address: Any, *args: Any):
        _check("udp", address)
        return originals[(socket.socket, "sendto")](self, data, address, *args)

    originals[(socket, "getaddrinfo")] = socket.getaddrinfo
    originals[(socket, "socketpair")] = socket.socketpair
    socket.socketpair = socketpair  # type: ignore[assignment]
    originals[(socket.socket, "connect")] = socket.socket.connect
    originals[(socket.socket, "connect_ex")] = socket.socket.connect_ex
    originals[(socket.socket, "sendto")] = socket.socket.sendto
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = sendto  # type: ignore[method-assign]


def _patch_redirects(originals: Mapping[tuple[Any, str], Any]) -> None:
    try:
        from urllib.request import HTTPRedirectHandler
    except ImportError:
        return
    original = HTTPRedirectHandler.redirect_request
    originals[(HTTPRedirectHandler, "redirect_request")] = original

    def redirect_request(self: Any, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str, *args: Any, **kwargs: Any):
        host, port = _endpoint_parts(newurl)
        allowed = _policy_bool(_POLICY, "redirects", _policy_bool(_POLICY, "allow_redirects", False)) and _allowed_destination(host, port or 80)
        _record("redirect", host=host, port=port, allowed=allowed)
        if not allowed:
            raise NetworkPolicyError(f"network policy denied redirect destination {host}")
        return originals[(HTTPRedirectHandler, "redirect_request")](self, req, fp, code, msg, headers, newurl, *args, **kwargs)

    HTTPRedirectHandler.redirect_request = redirect_request  # type: ignore[method-assign]


def _validated_descendant_owner() -> bool:
    """Return whether a manifest names a real native-network owner.

    Python hooks cannot observe sockets opened by a native descendant.  The
    only honest escape hatch is an explicitly validated proxy/broker or an OS
    network sandbox that owns that descendant and emits evidence itself.
    """
    raw = _POLICY.get("descendant_enforcement", _POLICY.get("native_descendants"))
    if not isinstance(raw, Mapping):
        return False
    kind = str(raw.get("kind", raw.get("owner", ""))).lower()
    if kind not in {"proxy", "broker", "os", "os_firewall", "sandbox"}:
        return False
    if not bool(raw.get("validated")) or not bool(raw.get("observable")):
        return False
    # A declaration alone is not a route.  Require an explicit endpoint or a
    # host-issued broker descriptor, plus a named allowlisted route/wrapper.
    endpoint = _POLICY.get("proxy") or _POLICY.get("broker")
    return bool(endpoint) and bool(raw.get("route")) and bool(raw.get("wrapper"))


def _command_label(command: Any) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command[:3])
    return str(command)


_WINDOWS = os.name == "nt"


def _program_name(token: str) -> str:
    """Name of the program a command starts, as the allow rules spell it.

    On Windows the resolved program is ``ffprobe.exe`` or ``C:\\...\\git.exe``; the rules
    name ``ffprobe`` and ``git``. Elsewhere the name is taken exactly as before.
    """
    if not _WINDOWS:
        return Path(token).name.lower()
    name = str(token).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _local_media_tool(command: Any) -> bool:
    """Allow only offline ffmpeg/ffprobe descendants with local arguments.

    VibeComfy's VHS video writer legitimately launches ffmpeg after the
    Python child has connected to the allowlisted Comfy endpoint.  The native
    descendant hook cannot observe sockets opened by that binary, so permit
    this narrow, auditable case only when the executable is a media tool and
    none of its arguments names a URL or a network protocol.  Every other
    native descendant remains fail-closed behind the broker/OS-owner gate.
    """
    if isinstance(command, (str, bytes)):
        tokens = str(command).split()
    elif isinstance(command, (list, tuple)):
        tokens = [str(value) for value in command]
    else:
        return False
    if not tokens:
        return False
    executable = _program_name(tokens[0])
    if executable not in {"ffmpeg", "ffprobe"}:
        return False
    network_prefixes = ("http://", "https://", "rtmp://", "rtsp://", "tcp:", "udp:")
    return not any(
        token.lower().startswith(network_prefixes) or "://" in token.lower()
        for token in tokens[1:]
    )


def _local_identity_tool(command: Any) -> bool:
    """Allow only read-only Git identity probes used by attestation.

    Managed VibeComfy sessions re-check their already-attested checkout while
    preparing a run.  Those probes are native descendants, but they never
    contact a remote: permitting only the exact metadata subcommands keeps the
    descendant boundary fail-closed for fetch/clone/push and arbitrary Git
    execution.
    """
    if not isinstance(command, (list, tuple)):
        return False
    tokens = [str(value) for value in command]
    if not tokens or _program_name(tokens[0]) != "git":
        return False
    # ``git -c key=value`` is a configuration override; ``ls-files -c`` is a
    # harmless cache selector and is one of the attestation probes below.
    if len(tokens) > 1 and tokens[1] == "-c":
        return False
    if len(tokens) > 1 and any(token.startswith("--config") for token in tokens[1:]):
        return False
    # VibeComfy/Astrid use these probes to bind a managed source checkout.
    allowed = {
        ("rev-parse", "HEAD"),
        ("rev-parse", "--show-toplevel"),
        ("ls-files", "-z", "-c", "-o", "--exclude-standard"),
        ("status", "--porcelain", "--untracked-files=all"),
    }
    # Accept an optional local checkout selector, but never a remote URL.
    if len(tokens) >= 3 and tokens[1] == "-C":
        tokens = [tokens[0], *tokens[2:]]
    return tuple(tokens[1:]) in allowed


def _local_process_probe(command: Any) -> bool:
    """Allow the narrow local listener probe used by managed sessions."""
    if not isinstance(command, (list, tuple)):
        return False
    tokens = [str(value) for value in command]
    if len(tokens) != 6 or Path(tokens[0]).name.lower() != "lsof":
        return False
    if tokens[1:4] != ["-nP", "-t", "-a"] or tokens[5] != "-sTCP:LISTEN":
        return False
    return tokens[4].startswith("-iTCP:") and tokens[4][7:].isdigit()


def _patch_native_descendants(originals: MutableMapping[tuple[Any, str], Any]) -> None:
    """Fail closed when a Python provider tries to escape via a native child."""
    original_popen = subprocess.Popen

    def check(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> None:
        command = args[0] if args else kwargs.get("args", "")
        local_media = _local_media_tool(command)
        local_identity = _local_identity_tool(command)
        local_process = _local_process_probe(command)
        safe_invocation = not bool(kwargs.get("shell")) and not kwargs.get("executable")
        allowed = safe_invocation and (
            _validated_descendant_owner() or local_media or local_identity or local_process
        )
        _record(
            "native_descendant",
            allowed=allowed,
            detail=(
                "local-media-tool:" if local_media else
                "local-identity-tool:" if local_identity else
                "local-process-probe:" if local_process else ""
            ) + _command_label(command),
        )
        if not allowed:
            raise NetworkPolicyError(
                "network policy denied native descendant; use a validated observable proxy or OS broker"
            )

    # A class, not a function: the standard library subclasses subprocess.Popen
    # (asyncio.windows_utils does at import on Windows), and callers use isinstance.
    class GuardedPopen(original_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            check(args, kwargs)
            super().__init__(*args, **kwargs)

    GuardedPopen.__name__ = GuardedPopen.__qualname__ = "Popen"

    originals[(subprocess, "Popen")] = original_popen
    subprocess.Popen = GuardedPopen  # type: ignore[misc]

    for name in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe"):
        original = getattr(os, name, None)
        if original is None:
            continue

        def guarded_exec(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any):
            allowed = _validated_descendant_owner()
            _record("native_descendant", allowed=allowed, detail=f"os.{_name}")
            if not allowed:
                raise NetworkPolicyError(
                    "network policy denied native exec; use a validated observable proxy or OS broker"
                )
            return _original(*args, **kwargs)

        originals[(os, name)] = original
        setattr(os, name, guarded_exec)


def _broker_handshake() -> None:
    """Perform the live admission handshake for an explicitly configured proxy."""
    raw_proxy = _POLICY.get("proxy")
    if not isinstance(raw_proxy, str) or not raw_proxy:
        return
    parsed = urlsplit(raw_proxy)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise NetworkPolicyError("network policy proxy must be an explicit host:port URL")
    admission_digest = str(_ADMISSION.get("admission_digest", "")) if "admission_digest" in _ADMISSION else hashlib.sha256(
        json.dumps(_ADMISSION, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    nonce = str(_ADMISSION.get("network_nonce") or _ADMISSION.get("nonce") or "")
    auth_token = os.environ.get("ASTRID_NETWORK_BROKER_TOKEN", "")
    if (len(admission_digest) != 64 or any(c not in "0123456789abcdef" for c in admission_digest)
            or not all(value and value.isascii() and len(value) <= 256
                       and all(c.isalnum() or c in "_-" for c in value) for value in (nonce, auth_token))):
        raise NetworkPolicyError("network policy broker admission material is malformed")
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=3) as connection:
            # The digest binds the complete host admission; the nonce makes a
            # replayed handshake from another attempt fail closed.
            connection.sendall(f"ASTRID-BROKER/1 HELLO {admission_digest} {nonce} {auth_token}\n".encode("ascii"))
            response = connection.recv(64).decode("ascii", "replace").strip()
    except OSError as exc:
        _record("broker_handshake", host=parsed.hostname, port=parsed.port, allowed=False, detail=str(exc))
        raise NetworkPolicyError("network policy broker handshake failed") from exc
    allowed = response == "ASTRID-BROKER/1 OK"
    _record("broker_handshake", host=parsed.hostname, port=parsed.port, allowed=allowed)
    if not allowed:
        raise NetworkPolicyError("network policy broker rejected admission")


def install(policy: Mapping[str, Any], evidence_path: str | Path, *, admission: Mapping[str, Any] | None = None, evidence_key: str = "") -> None:
    """Install hooks in a child process and arrange structured evidence output."""
    global _INSTALLED, _POLICY, _EVIDENCE, _EVIDENCE_KEY, _ADMISSION
    if _INSTALLED:
        return
    _POLICY = dict(policy)
    _EVIDENCE = Path(evidence_path)
    # ``evidence_key`` remains an ignored compatibility parameter for callers
    # on the old seam.  It is intentionally never retained in a child.
    del evidence_key
    _EVIDENCE_KEY = ""
    _ADMISSION = dict(admission or {})
    originals: dict[tuple[Any, str], Any] = {}
    _patch_socket(originals)
    _patch_redirects(originals)
    _patch_native_descendants(originals)
    _broker_handshake()
    _INSTALLED = True
    atexit.register(_write_evidence)


def install_from_environment(*, strict: bool = False, broker_required: bool = False) -> bool:
    """Return proven installation; strict callers treat absence as fatal."""
    raw = os.environ.get("ASTRID_NETWORK_POLICY")
    evidence = os.environ.get("ASTRID_NETWORK_EVIDENCE")
    if not raw or not evidence:
        if strict:
            raise NetworkPolicyError("network policy startup configuration is missing")
        return False
    try:
        policy = json.loads(raw)
    except ValueError as exc:
        if strict:
            raise NetworkPolicyError("network policy startup configuration is malformed") from exc
        return False
    if strict and (not isinstance(policy, Mapping) or broker_required and not policy.get("proxy")):
        raise NetworkPolicyError("network policy startup configuration cannot install required broker")
    admission_raw = os.environ.get("ASTRID_NETWORK_ADMISSION", "{}")
    try:
        admission = json.loads(admission_raw)
    except ValueError:
        admission = {}
    if "ASTRID_NETWORK_ADMISSION" not in os.environ:
        admission = {"admission_digest": os.environ.get("ASTRID_NETWORK_ADMISSION_DIGEST", ""),
                     "network_nonce": os.environ.get("ASTRID_NETWORK_NONCE", "")}
    # This token authenticates the child to a host-owned broker.  It is not
    # the broker's evidence signing secret.
    key = os.environ.get("ASTRID_NETWORK_BROKER_TOKEN", "")
    if isinstance(policy, Mapping) and isinstance(admission, Mapping):
        install(policy, evidence, admission=admission, evidence_key=key)
        return _INSTALLED
    if strict:
        raise NetworkPolicyError("network policy startup admission is malformed")
    return False


@contextmanager
def resident_job_policy(policy: Mapping[str, Any] | None, evidence_path: Path,
                        *, admission: Mapping[str, Any]):
    """Bind the existing Python hook to one serial resident job, then revoke it.

    Broker/native/delegation policies still use disposable workers. This adds
    no network route and gives idle/background code no new-connect authority.
    Child observations remain diagnostics; only the parent can publish results.
    """
    global _RESIDENT_JOB, _POLICY, _EVIDENCE, _ADMISSION
    if _RESIDENT_JOB:
        raise NetworkPolicyError("resident network context is already occupied")
    selected = dict(policy or {"allowed_protocols": [], "allowed_destinations": []})
    if selected.get("broker") or selected.get("proxy"):
        raise NetworkPolicyError("brokered work requires a disposable worker")
    install({"allowed_protocols": [], "allowed_destinations": []}, evidence_path)
    with _LOCK:
        _RESIDENT_JOB = True
        _POLICY, _EVIDENCE, _ADMISSION = selected, Path(evidence_path), dict(admission)
        _DNS_NAMES.clear()
        _EVENTS.clear()
    try:
        yield
    finally:
        try:
            _write_evidence()
        finally:
            with _LOCK:
                _POLICY = {"allowed_protocols": [], "allowed_destinations": []}
                _EVIDENCE, _ADMISSION = None, {}
                _DNS_NAMES.clear()
                _EVENTS.clear()
                _RESIDENT_JOB = False


__all__ = ["NetworkPolicyError", "install", "install_from_environment"]
