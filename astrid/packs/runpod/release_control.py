"""Private loopback handoff to the retained RunPod account owner.

The account lock intentionally belongs to the long-lived host. A short-lived
CLI can request an exact release through this channel without opening a second
owner or competing for that lock.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import stat
import struct
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

_MAX_MESSAGE = 4096
_SCHEMA = "astrid.runpod.release-control.v1"


def descriptor_path(claim_path: Path) -> Path:
    return claim_path.with_name(claim_path.name + ".release-control")


def _recv_exact(sock: socket.socket, size: int, *, deadline: float | None = None) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("release-control frame deadline passed")
            sock.settimeout(remaining)
        block = sock.recv(size - len(chunks))
        if not block:
            raise OSError("release-control peer closed early")
        chunks.extend(block)
    return bytes(chunks)


def _send_json(sock: socket.socket, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_MESSAGE:
        raise ValueError("release-control message exceeds limit")
    sock.sendall(struct.pack("!I", len(encoded)) + encoded)


def _recv_json(sock: socket.socket, *, deadline: float | None = None) -> dict[str, Any]:
    size = struct.unpack("!I", _recv_exact(sock, 4, deadline=deadline))[0]
    if size < 1 or size > _MAX_MESSAGE:
        raise ValueError("release-control message size is invalid")
    value = json.loads(_recv_exact(sock, size, deadline=deadline))
    if not isinstance(value, dict):
        raise ValueError("release-control message is not an object")
    return value


class ReleaseControlServer:
    """Single-owner bounded request server, bound only to loopback."""

    def __init__(self, claim_path: Path, owner_epoch: str,
                 handler: Callable[[str, str], Mapping[str, Any]]) -> None:
        self.path = descriptor_path(claim_path)
        self.owner_epoch = owner_epoch
        self.nonce = secrets.token_hex(32)
        self.handler = handler
        self._closed = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._descriptor_identity: tuple[int, int] | None = None
        self._thread = threading.Thread(target=self._serve, name="runpod-release-control", daemon=True)
        try:
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            self._socket.bind(("127.0.0.1", 0))
            self._socket.listen(4)
            self._socket.settimeout(0.25)
            self._write_descriptor()
            self._thread.start()
        except Exception:  # noqa: BLE001 - release every partially-created local resource before propagating startup failure
            self._socket.close()
            self._remove_descriptor()
            raise

    def _write_descriptor(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps({
            "schema": _SCHEMA,
            "host": "127.0.0.1",
            "port": self._socket.getsockname()[1],
            "owner_epoch": self.owner_epoch,
            "nonce": self.nonce,
        }, separators=(",", ":")).encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=".runpod-release-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            stream = os.fdopen(fd, "wb")
            fd = -1  # The stream owns it; its number may be reused after close.
            with stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            info = self.path.lstat()
            self._descriptor_identity = (info.st_dev, info.st_ino)
            os.chmod(self.path, 0o600)
        except (OSError, ValueError):
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._closed.is_set():
                    return
                continue
            with connection:
                # A peer that connects and drips bytes must not monopolize the
                # owner's only listener. One deadline covers header and payload;
                # a valid request's release handler may itself take longer.
                connection.settimeout(2.0)
                try:
                    request = _recv_json(connection, deadline=time.monotonic() + 2.0)
                    if set(request) != {"schema", "nonce", "owner_epoch", "operation_id", "pod_id"}:
                        raise ValueError("release-control request fields are invalid")
                    if (request["schema"] != _SCHEMA
                            or not isinstance(request["nonce"], str)
                            or not hmac.compare_digest(request["nonce"], self.nonce)
                            or request["owner_epoch"] != self.owner_epoch
                            or not isinstance(request["operation_id"], str)
                            or not isinstance(request["pod_id"], str)):
                        raise ValueError("release-control request authentication failed")
                    result = self.handler(request["operation_id"], request["pod_id"])
                    response = {"ok": True, "receipt": dict(result)}
                except Exception as exc:  # noqa: BLE001 - hide provider details at this local protocol boundary
                    response = {"ok": False, "error": f"release request failed ({type(exc).__name__})"}
                try:
                    _send_json(connection, response)
                except (OSError, ValueError):
                    continue

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._socket.close()
        self._thread.join(timeout=1.0)
        self._remove_descriptor()

    def _remove_descriptor(self) -> None:
        try:
            info = self.path.lstat()
            if (self._descriptor_identity is not None and stat.S_ISREG(info.st_mode)
                    and (info.st_dev, info.st_ino) == self._descriptor_identity):
                self.path.unlink()
        except (FileNotFoundError, OSError):
            pass


def request_release(claim_path: Path, operation_id: str, pod_id: str) -> Mapping[str, Any]:
    """Ask the current retained owner to release one exact journal identity."""
    path = descriptor_path(claim_path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("retained RunPod release endpoint is not private")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError("retained RunPod release endpoint has a different owner")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            encoded = stream.read(_MAX_MESSAGE + 1)
            if len(encoded) > _MAX_MESSAGE:
                raise RuntimeError("retained RunPod release endpoint descriptor exceeds limit")
            descriptor = json.loads(encoded)
    finally:
        if fd >= 0:
            os.close(fd)
    if (not isinstance(descriptor, dict) or descriptor.get("schema") != _SCHEMA
            or descriptor.get("host") != "127.0.0.1"
            or type(descriptor.get("port")) is not int or not 1 <= descriptor["port"] <= 65535
            or not isinstance(descriptor.get("owner_epoch"), str)
            or not isinstance(descriptor.get("nonce"), str)):
        raise RuntimeError("retained RunPod release endpoint descriptor is invalid")
    with socket.create_connection(("127.0.0.1", descriptor["port"]), timeout=40.0) as connection:
        connection.settimeout(40.0)
        _send_json(connection, {
            "schema": _SCHEMA,
            "nonce": descriptor["nonce"],
            "owner_epoch": descriptor["owner_epoch"],
            "operation_id": operation_id,
            "pod_id": pod_id,
        })
        response = _recv_json(connection)
    if response.get("ok") is not True or not isinstance(response.get("receipt"), dict):
        raise RuntimeError(str(response.get("error", "retained RunPod release failed")))
    return response["receipt"]
