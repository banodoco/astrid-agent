"""One private, source-bound preparation operation in an existing pack host.

Launch configuration is authority; wire context is only correlation. This uses
Darwin's existing audit-token verifier and deliberately has no UID-only mode.
The readiness record is a binding, never a quote cache or execution authority.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import socket
import stat
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import custody_broker as custody
from .storage_estimator import StorageEstimateRequest, StoragePreparation

PROTOCOL = "astrid.storage-preparation/v1"
COMMAND = "prepare_storage_estimate"
# Effective end-to-end private wire ceiling, including the UTF-8 newline.
FRAME_LIMIT = 64 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class PreparationTransportError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    uid: int
    birth_id: str
    audit_token_sha256: str
    audit_token_pidversion: int

    def __post_init__(self):
        if (type(self.pid) is not int or self.pid <= 0
                or type(self.uid) is not int or self.uid < 0
                or type(self.birth_id) is not str or not self.birth_id
                or type(self.audit_token_pidversion) is not int or self.audit_token_pidversion <= 0
                or type(self.audit_token_sha256) is not str
                or not _DIGEST.fullmatch(self.audit_token_sha256)):
            raise PreparationTransportError("identity_unavailable")

    def matches(self, observed):
        if observed != asdict(self):
            raise PreparationTransportError("peer_mismatch")


@dataclass(frozen=True)
class StoragePreparationConfig:
    support_root: Path
    host_instance_id: str
    host_peer: ProcessIdentity
    runtime_peer: ProcessIdentity
    boot_manifest_hash: str
    runtime_realm_id: str
    runtime_instance_id: str
    runtime_session_id: str
    executor_id: str
    source_epoch: str
    capability_id: str
    capability_digest: str
    source_digest: str
    dependency_digest: str
    estimator_id: str
    estimator_version: int
    estimator_implementation_digest: str
    runtime_epoch: int
    executor_source_digest: str
    executor_dependency_digest: str

    def __post_init__(self):
        if (not isinstance(self.support_root, Path)
                or not self.support_root.is_absolute()
                or type(self.host_peer) is not ProcessIdentity
                or type(self.runtime_peer) is not ProcessIdentity):
            raise PreparationTransportError("invalid_config")
        for key in ("host_instance_id", "runtime_realm_id", "runtime_instance_id",
                    "runtime_session_id", "executor_id", "source_epoch", "capability_id", "estimator_id"):
            if type(getattr(self, key)) is not str or not getattr(self, key):
                raise PreparationTransportError("invalid_config")
        if (type(self.boot_manifest_hash) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", self.boot_manifest_hash)):
            raise PreparationTransportError("invalid_config")
        for key in ("capability_digest", "source_digest", "dependency_digest",
                    "estimator_implementation_digest", "executor_source_digest", "executor_dependency_digest"):
            if type(getattr(self, key)) is not str or not _DIGEST.fullmatch(getattr(self, key)):
                raise PreparationTransportError("invalid_config")
        if (type(self.runtime_epoch) is not int or self.runtime_epoch <= 0
                or type(self.estimator_version) is not int or self.estimator_version <= 0):
            raise PreparationTransportError("invalid_config")

    @property
    def context(self):
        return {"runtime": {"realm_id": self.runtime_realm_id,
                "runtime_instance_id": self.runtime_instance_id,
                "runtime_session_id": self.runtime_session_id, "runtime_epoch": self.runtime_epoch},
                "executor_id": self.executor_id, "source_epoch": self.source_epoch,
                "capability_id": self.capability_id, "capability_digest": self.capability_digest,
                "storage_estimate_contract": {"mode": "request_estimated", "estimator": self.estimator}}

    @property
    def estimator(self):
        return {"id": self.estimator_id, "version": self.estimator_version,
                "implementation_digest": self.estimator_implementation_digest}

    @property
    def socket_path(self):
        return self.support_root / "storage-preparation" / "s"

    @property
    def readiness_path(self):
        return self.socket_path.parent / "ready.json"


def validate_host_binding(host, config):
    """Validate admitted source/declaration without invoking the estimator."""
    from .generic_host import (
        _admitted_source_roots,
        _canonical_digest,
        _capability_digest,
        _dependency_digest,
        _is_withdrawn,
        _verify_admitted_source,
    )
    from .storage_estimator import estimator_descriptor
    if (host._shutdown.is_set() or host.executor_id != config.executor_id
            or host.source_epoch != config.source_epoch or host.source_epoch == "uninitialized"
            or "sha256:" + _canonical_digest({k: v.source_digest for k, v in host.capabilities.items()}) != config.executor_source_digest
            or "sha256:" + _canonical_digest({k: v.dependency_digest for k, v in host.capabilities.items()}) != config.executor_dependency_digest
            or any(host._registered_runtime_state.get(k) != v for k, v in config.context["runtime"].items() if k != "realm_id")
            or host.boot_manifest_path is None or host.boot_manifest_hash != config.boot_manifest_hash
            or not host.boot_manifest_path.is_relative_to(config.support_root)
            or host.boot_manifest_provenance() != {"kind": "astrid.boot_manifest", "sha256": config.boot_manifest_hash}
            or sys.platform != "darwin" or not shutil.which("/usr/bin/sandbox-exec")):
        raise PreparationTransportError("binding_unavailable")
    record = host.capabilities.get(config.capability_id)
    if (record is None or record.approval_reason or _is_withdrawn(record)
            or not record.has_request_storage_estimator
            or record.capability_digest != config.capability_digest
            or "sha256:" + record.source_digest != config.source_digest
            or "sha256:" + record.dependency_digest != config.dependency_digest
            or _dependency_digest(record.definition, host.capabilities) != record.dependency_digest
            or _capability_digest(record.definition.to_dict()) != config.capability_digest):
        raise PreparationTransportError("source_mismatch")
    descriptor = estimator_descriptor(record)
    if {key: descriptor[key] for key in ("id", "version", "implementation_digest")} != config.estimator:
        raise PreparationTransportError("estimator_unavailable")
    # Same no-input frontier already enforced by J038's execution boundary.
    if (record.definition.metadata.get("hc04_cas_param_ports")
            or any(port.type == "file" for port in record.definition.inputs)):
        raise PreparationTransportError("input_facts_unavailable")
    _verify_admitted_source({"source_digest": record.source_digest,
        "source_roots": [str(p) for p in _admitted_source_roots(record.source_root, record.definition)]})


def _encoded(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise PreparationTransportError("malformed_frame") from exc
    if len(raw) > FRAME_LIMIT:
        raise PreparationTransportError("oversized_frame")
    return raw


def _remaining(channel, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PreparationTransportError("timeout")
    channel.settimeout(remaining)


def read_frame(channel, deadline):
    # Reuse the strict Host parser (duplicate keys, constants and trailing frame
    # rejection), with a total deadline rather than a fresh timeout per recv.
    from .generic_host import HostError, _read_activation_frame

    class DeadlineReader:
        def recv(self, count):
            _remaining(channel, deadline)
            return channel.recv(count)
    try:
        return _read_activation_frame(DeadlineReader(), limit=FRAME_LIMIT)
    except HostError as exc:
        raise PreparationTransportError("malformed_frame") from exc


def _directory(path):
    """Open each component without following links, retaining the final fd."""
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        raise PreparationTransportError("unsafe_endpoint")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        facts = os.fstat(fd)
        if facts.st_uid != os.getuid() or stat.S_IMODE(facts.st_mode) != 0o700:
            raise PreparationTransportError("unsafe_endpoint")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _inode(facts):
    return facts.st_dev, facts.st_ino


class StoragePreparationListener:
    """A single serial listener thread owned and closed by GenericPackHost."""

    def __init__(self, host, config: StoragePreparationConfig):
        if type(config) is not StoragePreparationConfig:
            raise PreparationTransportError("invalid_config")
        self.host, self.config = host, config
        self.timeout = host.execution_policy.deadline_seconds
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise PreparationTransportError("invalid_deadline")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active = None
        self._listener = None
        self._thread = None
        self._root_fd = None
        self._owned = {}
        self.error = None

    def _verify_processes(self):
        self.config.host_peer.matches(self._host_actor.verify())
        self.config.runtime_peer.matches(self._runtime_actor.verify())

    def start(self):
        config = self.config
        if self._listener is not None or self._stop.is_set():
            raise PreparationTransportError("unavailable")
        try:
            if config.host_peer.pid != os.getpid() or config.host_peer.uid != os.getuid():
                raise PreparationTransportError("peer_mismatch")
            self._host_actor = custody.AuthenticatedCleanupActor.current()
            self._runtime_actor = custody.AuthenticatedCleanupActor(
                lambda: custody.current_process_audit_token(config.runtime_peer.pid), custody.default_process_identity)
            self._verify_processes()
            validate_host_binding(self.host, config)
            support_fd = _directory(config.support_root)
            try:
                self._support_identity = _inode(os.fstat(support_fd))
                try:
                    os.mkdir("storage-preparation", mode=0o700, dir_fd=support_fd)
                except FileExistsError:
                    pass
                self._root_fd = _directory(config.socket_path.parent)
            finally:
                os.close(support_fd)
            self._root_identity = _inode(os.fstat(self._root_fd))
            if len(os.fsencode(config.socket_path)) > custody.DARWIN_UNIX_SOCKET_PATH_MAX_BYTES:
                raise PreparationTransportError("unsafe_endpoint")
            listener = self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(config.socket_path))  # Never unlink/reuse an existing endpoint.
            self._owned["s"] = _inode(os.lstat(config.socket_path))
            os.chmod(config.socket_path, 0o600, follow_symlinks=False)
            listener.listen(1)
            listener.settimeout(0.2)
            self._record = {"protocol": PROTOCOL, "command": COMMAND, "context": config.context,
                "host_instance_id": config.host_instance_id, "host_peer": asdict(config.host_peer),
                "runtime_peer": asdict(config.runtime_peer), "boot_manifest_hash": config.boot_manifest_hash,
                "socket_path": str(config.socket_path), "capability_id": config.capability_id,
                "capability_digest": config.capability_digest, "source_digest": config.source_digest,
                "dependency_digest": config.dependency_digest,
                "estimator": config.estimator,
                "executor_source_digest": config.executor_source_digest,
                "executor_dependency_digest": config.executor_dependency_digest}
            encoded = _encoded(self._record)
            fd = os.open("ready.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self._root_fd)
            try:
                self._owned["ready.json"] = _inode(os.fstat(fd))
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            self._validate_endpoint()
            self._thread = threading.Thread(target=self._serve, name="astrid-storage-preparation", daemon=True)
            self._thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def _validate_endpoint(self):
        support_fd = _directory(self.config.support_root)
        try:
            if _inode(os.fstat(support_fd)) != self._support_identity:
                raise PreparationTransportError("stale_endpoint")
        finally:
            os.close(support_fd)
        fd = _directory(self.config.socket_path.parent)
        try:
            if _inode(os.fstat(fd)) != self._root_identity:
                raise PreparationTransportError("stale_endpoint")
            for name, kind in (("s", stat.S_ISSOCK), ("ready.json", stat.S_ISREG)):
                facts = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (not kind(facts.st_mode) or facts.st_uid != os.getuid()
                        or stat.S_IMODE(facts.st_mode) != 0o600 or _inode(facts) != self._owned[name]):
                    raise PreparationTransportError("unsafe_endpoint")
            record_fd = os.open("ready.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                if _inode(os.fstat(record_fd)) != self._owned["ready.json"]:
                    raise PreparationTransportError("stale_endpoint")
                # Exact bytes also reject duplicate keys or stale/extra records.
                if os.read(record_fd, FRAME_LIMIT + 1) != _encoded(self._record):
                    raise PreparationTransportError("stale_endpoint")
            finally:
                os.close(record_fd)
        finally:
            os.close(fd)

    def _check_live(self):
        try:
            if self._stop.is_set() or self.host._shutdown.is_set():
                raise PreparationTransportError("unavailable")
            self._verify_processes()
            self._validate_endpoint()
            validate_host_binding(self.host, self.config)
        except Exception:
            self._remove_owned("ready.json")
            raise

    def is_ready(self):
        """Live composition probe; the record alone never proves liveness."""
        try:
            self._check_live()
            return self._thread is not None and self._thread.is_alive()
        except Exception:  # noqa: BLE001 - readiness is fail-closed at this boundary.
            return False

    def _request(self, frame):
        config = self.config
        if set(frame) != {"protocol", "command", "request_id", "context", "request"}:
            raise PreparationTransportError("invalid_request")
        if frame["protocol"] != PROTOCOL or frame["command"] != COMMAND:
            raise PreparationTransportError("unsupported_operation")
        if type(frame["request_id"]) is not str or not frame["request_id"]:
            raise PreparationTransportError("invalid_request")
        if _encoded(frame["context"]) != _encoded(config.context):
            raise PreparationTransportError("context_mismatch")
        body = frame["request"]
        if type(body) is not dict or set(body) != {
                "capability_id", "capability_digest", "schema_version", "spec", "input_facts"}:
            raise PreparationTransportError("invalid_request")
        if (body["capability_id"] != config.capability_id
                or body["capability_digest"] != config.capability_digest
                or type(body["schema_version"]) is not int or body["schema_version"] != 1
                or type(body["spec"]) is not dict or type(body["input_facts"]) is not list):
            raise PreparationTransportError("invalid_request")
        if (body["input_facts"] or any(body["spec"].get(key) for key in
                ("input_object_ids", "inputs", "input_digests", "runtime_dependencies", "timeline_snapshot"))):
            raise PreparationTransportError("input_facts_unavailable")
        return StorageEstimateRequest(body["capability_id"], body["capability_digest"],
                                      body["schema_version"], body["spec"], ())

    def _handle(self, channel):
        deadline = time.monotonic() + self.timeout
        peer = custody.AuthenticatedCleanupActor.private_peer(channel)
        self.config.runtime_peer.matches(peer.verify())  # Before parsing any claims.
        frame = read_frame(channel, deadline)
        # Untrusted correlation is echoed only after exact context validation.
        reply = {"protocol": PROTOCOL, "command": COMMAND,
                 "request_id": frame.get("request_id"), "context": self.config.context}
        try:
            request = self._request(frame)
            self._check_live()
            self.config.runtime_peer.matches(peer.verify())
            original_request = copy.deepcopy(request)
            prepared = self.host.prepare_storage_estimate(request)  # Exactly one delegation.
            self._check_live()
            self.config.runtime_peer.matches(peer.verify())
            if (type(prepared) is not StoragePreparation or prepared.request != original_request
                    or prepared.source_digest != self.config.source_digest
                    or prepared.estimator != self.config.estimator):
                raise PreparationTransportError("preparation_mismatch")
            reply["preparation"] = prepared.to_dict()  # Existing flat payload, unchanged.
        except Exception as exc:  # noqa: BLE001 - estimator failure must become a refusal.
            if not isinstance(exc, PreparationTransportError):
                # Public responses retain the closed refusal code. Keep bounded
                # internal diagnostics without echoing the request or its spec.
                print(json.dumps({"event": "storage_preparation_failed",
                                  "exception": type(exc).__name__,
                                  "message": str(exc)[:512]}), file=sys.stderr, flush=True)
            reply["refusal"] = {"code": exc.code if isinstance(exc, PreparationTransportError)
                                else "preparation_unavailable"}
        try:
            encoded = _encoded(reply)
        except PreparationTransportError as exc:
            if exc.code != "oversized_frame":
                raise
            reply.pop("preparation", None)
            reply["refusal"] = {"code": "oversized_frame"}
            encoded = _encoded(reply)
        _remaining(channel, deadline)  # Discard any late result, never pretend cancellation.
        channel.sendall(encoded)

    def _serve(self):
        try:
            while not self._stop.is_set():
                try:
                    channel, _ = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                with channel:
                    with self._lock:
                        if self._stop.is_set():
                            break
                        self._active = channel
                    try:
                        self._handle(channel)
                    except Exception as exc:  # noqa: BLE001 - isolate failed connections.
                        # Invalid peer/framing has no authenticated correlation.
                        self.error = type(exc).__name__
                    finally:
                        with self._lock:
                            self._active = None
        finally:
            self._remove_owned("ready.json")

    def _remove_owned(self, name):
        if self._root_fd is None or name not in self._owned:
            return
        try:
            facts = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            if _inode(facts) == self._owned[name]:
                os.unlink(name, dir_fd=self._root_fd)
        except FileNotFoundError:
            pass

    def close(self):
        self._stop.set()
        self._remove_owned("ready.json")
        if self._listener is not None:
            self._listener.close()
        with self._lock:
            if self._active is not None:
                try:
                    self._active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.timeout)
            if self._thread.is_alive():
                raise PreparationTransportError("shutdown_unresolved")
        self._remove_owned("s")
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None
