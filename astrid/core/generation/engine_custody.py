"""Installation-scoped Linux owner for trusted engine and finite commands.

One private Unix socket; authority stays in the live owner's native handles.
The session marker fences owner loss. It never authorizes PID adoption.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from concurrent.futures import CancelledError
from dataclasses import asdict, replace
import hashlib
import logging
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Callable

from runtime_protocol import platform as native

from astrid.core.execution.process_group import (
    process_birth_identity, held_child_birth_identity, _zombie_birth_identity,
)

from . import engine_recipe as recipes
from ._engine_python_bootstrap import engine_child_env, engine_process_env
from ._engine_target_files import _TargetFiles
from .engine_contracts import (
    CommandRequest, CommandResult, CustodyUnresolved, DeadlineExceeded,
    EndpointHealthError, EndpointOwnershipError, EngineError, IntegrityError,
    RecipeValidationError, UnsafePathError, UnsupportedPlatform, effective_models_root,
    custody_message, engine_reason,
)
from .engine_runner import _json, _object_info_sha256, _root, _sha, helper_manifest
from .engine_platforms import LinuxPlatformAdapter

_SCHEMA = "astrid.engine-service.v1"
_FRAME_LIMIT = 16 * 1024 * 1024
_OUTPUT_LIMIT = 1024 * 1024
_CLEANUP_SECONDS = 8.0
_CAUSE_LIMIT = 600
_CLEANUP_STAGES = {"terminate", "verify", "stdout_drain", "stderr_drain",
                   "stdout_close", "stderr_close", "drain"}
_SECRET_KEY = re.compile(r"token|secret|password|authorization|credential|bearer|key$|owner_id", re.I)
_ERRORS = {e.code: e for e in (CustodyUnresolved, DeadlineExceeded, EndpointHealthError,
           EndpointOwnershipError, IntegrityError, RecipeValidationError, UnsafePathError)}


def _cleanup_failure(stage, exc, secrets=()):
    """Only bounded diagnostic text crosses the owner boundary, never env/argv."""
    try:
        message = str(exc)
    except BaseException:
        message = "exception message unavailable"
    for value in sorted(set(secrets), key=len, reverse=True):
        if value:
            message = message.replace(value, "<redacted>")
    message = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1<redacted>@", message)
    message = re.sub(
        r'''(?i)(\b[\w-]*(?:token|secret|password|authorization|credential|api[_-]?key|owner_id|key)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|(?:bearer\s+)?[^\s,;&}]+)''',
        r"\1<redacted>", message)
    message = re.sub(r'''(?i)(\bbearer\s+)[^\s,;"'&}]+''', r"\1<redacted>", message)
    # Also suppress unlabelled opaque tokens, including launch IDs and JWTs.
    message = re.sub(r"\b[A-Za-z0-9_-]{24,}(?:\.[A-Za-z0-9_-]+)*\b", "<redacted>", message)
    message = " ".join(message.split())[:_CAUSE_LIMIT]
    return {"stage": stage, "exception_type": type(exc).__name__[:80], "message": message}


def _valid_cleanup_failure(value):
    return (isinstance(value, dict) and set(value) == {"stage", "exception_type", "message"}
            and isinstance(value["stage"], str) and value["stage"] in _CLEANUP_STAGES
            and isinstance(value["exception_type"], str) and 0 < len(value["exception_type"]) <= 80
            and isinstance(value["message"], str) and len(value["message"]) <= _CAUSE_LIMIT)


def _remaining(deadline):
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise RecipeValidationError("deadline must be finite")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded("engine deadline exceeded", retryable=True)
    return remaining


def _decode(data):
    def invalid(_value):
        raise ValueError("nonfinite JSON")
    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON")
        return result
    return json.loads(data, parse_constant=invalid, parse_float=finite)


def _send(channel, value, deadline):
    data = _json(value).encode()
    if len(data) > _FRAME_LIMIT:
        raise IntegrityError("owner frame exceeds limit")
    channel.settimeout(_remaining(deadline))
    channel.sendall(struct.pack("!I", len(data)) + data)


def _receive(channel, deadline, cancelled: Callable[[], bool] | None = None):
    def read(size):
        data = bytearray()
        while len(data) < size:
            if cancelled is not None and cancelled():
                raise CancelledError("engine operation cancelled")
            channel.settimeout(min(0.05, _remaining(deadline)) if cancelled is not None else _remaining(deadline))
            try:
                block = channel.recv(min(65536, size - len(data)))
            except socket.timeout:
                if cancelled is None:
                    raise
                continue
            if not block:
                raise EOFError
            data.extend(block)
        return bytes(data)
    size = struct.unpack("!I", read(4))[0]
    if not 0 < size <= _FRAME_LIMIT:
        raise IntegrityError("invalid owner frame size")
    return _decode(read(size))


def _private(info, kind):
    if (not kind(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise UnsafePathError("engine control state must be private to its owner")


@contextmanager
def _directory(path, *, create=True):
    """Pin a no-symlink directory; only the final component must be private."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        parts = Path(path).parts[1:]
        for component in parts:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            os.close(fd)
            fd = child
            if component == ".astrid-engine":
                _private(os.fstat(fd), stat.S_ISDIR)
        _private(os.fstat(fd), stat.S_ISDIR)
        yield fd
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise UnsafePathError("unsafe engine control directory") from exc
    finally:
        os.close(fd)


def _write(fd, name, data):
    temporary = "." + uuid.uuid4().hex
    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(out, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def _exists(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _socket_path(fd):
    # The fd stays open through bind/connect. This also supports long install roots.
    return f"/proc/self/fd/{fd}/control.sock"


def _supervisor_argv(identity, lock_fd, token):
    # Retained installed sources, never a disposable extracted helper tree.
    roots = [str(Path(__file__).resolve().parents[3]), str(Path(native.__file__).resolve().parent.parent)]
    program = f"import sys; sys.path[:0]={roots!r}; from astrid.core.generation.engine_custody import main; main()"
    return [sys.executable, "-I", "-c", program, "_serve", _json(identity),
            "--lock-fd", str(lock_fd), "--launch-token", token]


class LinuxEngineServiceClient:
    """Agreed v2 API. Construction performs no filesystem/process/network I/O."""

    def __init__(self, *, root, engine, platform, recipe_sha256, helper_sha256):
        if not sys.platform.startswith("linux") or not platform.startswith("linux-"):
            raise UnsupportedPlatform("engine custody requires Linux")
        self.identity = dict(root=_root(root, platform), engine=engine, platform=platform,
                             recipe_sha256=recipe_sha256, helper_sha256=helper_sha256)
        if (not isinstance(engine, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", engine)
                or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                       for v in (recipe_sha256, helper_sha256))):
            raise RecipeValidationError("invalid installation identity")
        self.control = root + "/.astrid-engine/session"
        self._spawned = None
        self.cancelled: Callable[[], bool] | None = None

    @classmethod
    def for_recipe(cls, *, root, recipe, platform):
        """Build the installation identity for an existing recipe's supervisor."""
        return cls(root=root, engine=recipe.engine, platform=platform,
                   recipe_sha256=recipes.recipe_digest(recipe),
                   helper_sha256=_sha(helper_manifest(recipe)))

    @classmethod
    def for_stop(cls, *, root, engine, platform):
        """Build a stop-only client from the complete live identity in its marker."""
        checked_root = LinuxPlatformAdapter(platform).validate_root(root)
        if not isinstance(engine, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", engine):
            raise RecipeValidationError("invalid installation engine")
        control = checked_root + "/.astrid-engine/session"
        try:
            with _directory(control, create=False) as fd:
                marker = cls._marker(fd)
                identity = marker["identity"] if marker is not None else None
        except FileNotFoundError:
            return None
        except (CustodyUnresolved, UnsafePathError) as exc:
            diagnostic = cls(root=checked_root, engine=engine, platform=platform,
                             recipe_sha256="0" * 64, helper_sha256="0" * 64)
            raise diagnostic._refusal(type(exc), None, reason="marker_invalid") from exc
        if identity is None:
            return None
        if (set(identity) != {"root", "engine", "platform", "recipe_sha256", "helper_sha256"}
                or identity.get("root") != checked_root or identity.get("engine") != engine
                or identity.get("platform") != platform):
            diagnostic = cls(root=checked_root, engine=engine, platform=platform,
                             recipe_sha256="0" * 64, helper_sha256="0" * 64)
            raise diagnostic._refusal(CustodyUnresolved, {"identity": identity}, reason="marker_invalid")
        try:
            return cls(**identity)
        except RecipeValidationError as exc:
            diagnostic = cls(root=checked_root, engine=engine, platform=platform,
                             recipe_sha256="0" * 64, helper_sha256="0" * 64)
            raise diagnostic._refusal(CustodyUnresolved, {"identity": identity}, reason="marker_invalid") from exc

    @staticmethod
    def _marker(fd):
        marker_info = _exists(fd, "session.json")
        if marker_info is not None:
            _private(marker_info, stat.S_ISREG)
            marker_fd = os.open("session.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=fd)
            try:
                info = os.fstat(marker_fd)
                _private(info, stat.S_ISREG)
                if info.st_nlink != 1 or info.st_size > 16384:
                    raise CustodyUnresolved("invalid supervisor marker; explicit recovery required")
                data = os.read(marker_fd, 16385)
                if len(data) != info.st_size or (info.st_dev, info.st_ino) != (marker_info.st_dev, marker_info.st_ino):
                    raise CustodyUnresolved("supervisor marker changed; explicit recovery required")
                try:
                    marker = _decode(data)
                except (ValueError, UnicodeError):
                    raise CustodyUnresolved("invalid supervisor marker; explicit recovery required") from None
            finally:
                os.close(marker_fd)
            if (not isinstance(marker, dict) or not {"identity", "owner_id"} <= set(marker)
                    or set(marker) - {"identity", "owner_id", "process", "pod_id"}
                    or not isinstance(marker["identity"], dict)
                    or not isinstance(marker["owner_id"], str)
                    or re.fullmatch(r"[0-9a-f]{32}", marker["owner_id"]) is None):
                raise CustodyUnresolved("invalid supervisor marker; explicit recovery required")
            return marker
        return None

    def _details(self, marker, *, reason, owner_state="unknown", lock_state="unknown", evidence="none"):
        identity = marker.get("identity", {}) if isinstance(marker, dict) else {}
        if not isinstance(identity, dict):
            identity = {}
        helper = identity.get("helper_sha256", "")
        return dict(reason=reason, owner_state=owner_state, lock_state=lock_state, evidence=evidence,
                    root=self.identity["root"], engine=self.identity["engine"],
                    helper_sha256=helper[:12] if isinstance(helper, str) and re.fullmatch(r"[0-9a-f]{64}", helper) else "unknown",
                    marker_path=self.control + "/session.json")

    def _refusal(self, error_type, marker, **state):
        details = self._details(marker, **state)
        error = error_type(custody_message(details))
        error.reason = engine_reason(custody_message({"reason": details["reason"]}))
        error.custody_details = details
        logging.getLogger(__name__).warning("Engine custody refusal: %s", error)
        return error

    def _retire_dead_owner(self, fd, deadline):
        """Retire only a proved-dead owner, under its unchanged native lock.

        An unverified filesystem rules out both lock and process death evidence.
        """
        _remaining(deadline)
        marker = self._marker(fd)
        if marker is None:
            return False
        if not _flock_reliable(fd):
            raise self._refusal(CustodyUnresolved, marker, reason="owner_network_mount")
        try:
            lock = os.open("owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except FileNotFoundError as exc:
            raise self._refusal(CustodyUnresolved, marker, reason="owner_unknown") from exc
        try:
            info = os.fstat(lock)
            _private(info, stat.S_ISREG)
            if info.st_nlink != 1:
                raise UnsafePathError("unsafe engine owner lock")
            try:
                native.lock_file(lock, blocking=False)
            except BlockingIOError:
                return False
            state, evidence = _owner_process_state(marker)
            if state == "alive":
                return False
            if state != "dead" and "process" not in marker:
                state, evidence = "dead", "flock"
            if state != "dead":
                return False
            if self._marker(fd) != marker:
                raise self._refusal(CustodyUnresolved, marker, reason="owner_unknown")
            for name, kind in (("control.sock", stat.S_ISSOCK), ("session.json", stat.S_ISREG)):
                info = _exists(fd, name)
                if info is not None:
                    _private(info, kind)
                    os.unlink(name, dir_fd=fd)
            os.fsync(fd)
            details = self._details(marker, reason="owner_unknown", owner_state="dead",
                                    lock_state="available", evidence=evidence)
            details.pop("reason")
            logging.getLogger(__name__).warning("Engine custody handover: %s", _json(details))
            return True
        except UnsafePathError as exc:
            raise self._refusal(UnsafePathError, marker, reason="marker_invalid") from exc
        finally:
            os.close(lock)

    def _owner_refusal(self, fd, error_type, reason):
        marker = self._marker(fd)
        if not _flock_reliable(fd):
            return self._refusal(error_type, marker, reason="owner_network_mount")
        lock_state = "unknown"
        try:
            lock = os.open("owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                _private(os.fstat(lock), stat.S_ISREG)
                try:
                    native.lock_file(lock, blocking=False)
                    lock_state = "available"
                except BlockingIOError:
                    lock_state = "held"
            finally:
                os.close(lock)
        except FileNotFoundError:
            pass
        state, evidence = _owner_process_state(marker or {})
        return self._refusal(error_type, marker, reason=reason, owner_state=state,
                             lock_state=lock_state, evidence=evidence)

    def _connect(self, fd, deadline, *, start, cancelled=None):
        while True:
            _remaining(deadline)
            if cancelled is not None and cancelled():
                raise CancelledError("engine operation cancelled before connection")
            marker = self._marker(fd)
            identity = marker["identity"] if marker is not None else None
            if identity is not None:
                if identity != self.identity:
                    if self._retire_dead_owner(fd, deadline):
                        continue
                    raise self._owner_refusal(fd, EndpointOwnershipError, "identity_mismatch")
            info = _exists(fd, "control.sock")
            if info is not None:
                _private(info, stat.S_ISSOCK)
                channel = socket.socket(socket.AF_UNIX)
                try:
                    channel.settimeout(_remaining(deadline))
                    channel.connect(_socket_path(fd))
                    return channel
                except (ConnectionRefusedError, FileNotFoundError):
                    channel.close()
                    if self._retire_dead_owner(fd, deadline):
                        continue
                    raise self._owner_refusal(fd, CustodyUnresolved, "owner_unreachable") from None
                except BaseException:
                    channel.close()
                    raise
            if _exists(fd, "session.json") is not None:
                if self._retire_dead_owner(fd, deadline):
                    continue
                _private(_exists(fd, "session.json"), stat.S_ISREG)
                lock = os.open("owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                try:
                    try:
                        native.lock_file(lock, blocking=False)
                    except BlockingIOError:
                        pass
                    else:
                        raise self._owner_refusal(fd, CustodyUnresolved, "owner_unknown")
                finally:
                    os.close(lock)
                # A starting owner may still be importing its retained sources.
                time.sleep(min(0.02, _remaining(deadline)))
                continue
            if not start:
                return None
            lock = os.open("owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                           0o600, dir_fd=fd)
            try:
                _private(os.fstat(lock), stat.S_ISREG)
                if os.fstat(lock).st_nlink != 1:
                    raise UnsafePathError("unsafe engine owner lock")
                try:
                    native.lock_file(lock, blocking=False)
                except BlockingIOError:
                    time.sleep(min(0.02, _remaining(deadline)))
                    continue
                if _exists(fd, "session.json") is not None or _exists(fd, "control.sock") is not None:
                    continue
                token = uuid.uuid4().hex
                # The child inherits the locked open description. Closing the parent
                # copy (without LOCK_UN) leaves ownership with that live supervisor.
                self._spawned = subprocess.Popen(
                    _supervisor_argv(self.identity, lock, token), pass_fds=(lock,),
                    cwd=self.identity["root"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                    env=engine_process_env(dict(os.environ), self.identity["root"]),
                )
                _publish_owner_marker(fd, self.identity, token, self._spawned.pid)
            except BaseException as exc:
                if isinstance(exc, EngineError):
                    raise
                raise CustodyUnresolved("supervisor launch uncertain; explicit recovery required") from exc
            finally:
                os.close(lock)

    def _request(self, operation, payload, deadline, *, start=True, cancelled=None, expected_peer=None):
        _remaining(deadline)
        if cancelled is not None and cancelled():
            raise CancelledError("engine operation cancelled before connection")
        try:
            with _directory(self.control, create=start) as fd:
                try:
                    channel = self._connect(fd, deadline, start=start, **({"cancelled": cancelled} if cancelled is not None else {}))
                except DeadlineExceeded as exc:
                    if _exists(fd, "session.json") is not None:
                        raise CustodyUnresolved("supervisor unavailable; explicit recovery required") from exc
                    raise
                if channel is None:
                    return None
                with channel:
                    if cancelled is not None or expected_peer is not None:
                        peer = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        incarnation = (*peer, _vibe_birth(peer[0]))
                        if expected_peer is not None and incarnation != expected_peer:
                            raise CustodyUnresolved("supervisor incarnation changed during cancellation")
                        if peer[1] != os.getuid():
                            raise CustodyUnresolved("supervisor peer has a different user")
                    if cancelled is not None and cancelled():
                        raise CancelledError("engine operation cancelled before request")
                    _send(channel, {"identity": self.identity, "operation": operation, "payload": payload,
                                    "remaining_seconds": _remaining(deadline)}, deadline)
                    # Keep the connection open while target deadline cleanup is verified.
                    try:
                        response = _receive(channel, deadline if operation == "stop" else deadline + _CLEANUP_SECONDS,
                                            **({"cancelled": cancelled} if cancelled is not None else {}))
                    except CancelledError:
                        # EOF cancels only this request; the supervisor drains its
                        # retained command before taking another operation.
                        channel.shutdown(socket.SHUT_RDWR)
                        channel.close()
                        barrier = self._request("run", {"argv": [sys.executable, "-I", "-S", "-B", "-c", "pass"],
                            "cwd": self.identity["root"], "env": {}, "max_output_bytes": 1024},
                            time.monotonic() + _CLEANUP_SECONDS + 2, start=False, expected_peer=incarnation)
                        if not isinstance(barrier, dict) or barrier.get("exit_code") != 0 or barrier.get("timed_out") is not False:
                            raise CustodyUnresolved("cancelled engine command drainage is unconfirmed")
                        raise
                    except DeadlineExceeded as exc:
                        raise CustodyUnresolved("supervisor did not confirm command cleanup before the response deadline") from exc
                if (not isinstance(response, dict) or response.get("identity") != self.identity
                        or set(response) not in ({"identity", "result"}, {"identity", "error", "reason"},
                                                 {"identity", "error", "reason", "cleanup_failure"})):
                    raise IntegrityError("unbound supervisor response")
                if "error" in response:
                    code = response["error"]
                    reason = engine_reason(response["reason"])
                    if reason is None:
                        raise IntegrityError("unbound supervisor response")
                    cause = response.get("cleanup_failure")
                    if "cleanup_failure" in response and not _valid_cleanup_failure(cause):
                        raise IntegrityError("invalid supervisor cleanup diagnostic")
                    message = "engine supervisor: " + str(code)
                    if cause is not None:
                        message += f" ({cause['stage']}: {cause['exception_type']}: {cause['message']})"
                    error = _ERRORS.get(code, CustodyUnresolved)(message)
                    error.reason = reason
                    error.cleanup_failure = cause
                    raise error
                return response["result"]
        except (CustodyUnresolved, EndpointOwnershipError, UnsafePathError) as exc:
            if not hasattr(exc, "custody_details"):
                try:
                    with _directory(self.control, create=False) as fd:
                        reason = ("request_mismatch" if isinstance(exc, EndpointOwnershipError) else
                                  "marker_invalid" if isinstance(exc, UnsafePathError) else "cleanup_unconfirmed")
                        error = self._owner_refusal(fd, type(exc), reason)
                except (EngineError, OSError):
                    error = self._refusal(type(exc), None, reason="marker_invalid")
                cause = getattr(exc, "cleanup_failure", None)
                if exc.reason is not None:
                    error.reason = exc.reason
                error.cleanup_failure = cause
                if _valid_cleanup_failure(cause):
                    error.args = (str(error) + f" ({cause['stage']}: {cause['exception_type']}: {cause['message']})",)
                raise error from exc
            raise
        except FileNotFoundError:
            if not start:
                return None
            raise UnsafePathError("engine control directory missing") from None
        except (OSError, EOFError, ValueError) as exc:
            raise self._refusal(CustodyUnresolved, None, reason="owner_unreachable") from exc

    def call(self, operation, payload, *, deadline):
        if operation not in {"probe", "start", "health"}:
            raise RecipeValidationError("unsupported engine service operation")
        return self._request(operation, payload, deadline, **({"cancelled": self.cancelled} if self.cancelled is not None else {}))

    def run(self, request: CommandRequest) -> CommandResult:
        return CommandResult(**self._request("run", {"argv": list(request.argv), "cwd": request.cwd,
                             "env": dict(request.env), "max_output_bytes": request.max_output_bytes}, request.deadline, **({"cancelled": self.cancelled} if self.cancelled is not None else {})))

    def release_owned(self, *, deadline):
        return self._request("release", {}, deadline, start=False)

    def _live_owner(self):
        owner = type(self).for_stop(**{key: self.identity[key] for key in ("root", "engine", "platform")})
        return self if owner is None or owner.identity == self.identity else owner

    def stop_owned(self, *, deadline):
        owner = self._live_owner()
        try:
            owner._request("stop", {}, deadline, start=False)
            # Marker retirement precedes release of the inherited native lock.
            while Path(self.control, "session.json").exists():
                time.sleep(min(0.02, _remaining(deadline)))
            _wait_owner_lock(self.control, deadline)
        except (CustodyUnresolved, DeadlineExceeded) as exc:
            error = CustodyUnresolved("Cannot stop the original supervisor; restore its owner's control connection and retry installation; leave the marker in place")
            error.cleanup_failure = getattr(exc, "cleanup_failure", None)
            error.custody_details = getattr(exc, "custody_details", owner._details(
                {"identity": owner.identity}, reason="owner_lock_timeout",
                lock_state="held" if isinstance(exc, DeadlineExceeded) else "unknown"))
            raise error from exc
        if self._spawned is not None:
            self._spawned.wait(timeout=_remaining(deadline))


def _publish_owner_marker(fd, identity, token, pid):
    """Both launch and serving publish the same supervisor incarnation."""
    birth = held_child_birth_identity(pid)
    if not birth:
        raise CustodyUnresolved("supervisor process evidence unavailable; leave its control state in place")
    marker = {"identity": identity, "owner_id": token, "process": {"pid": pid, "birth_id": birth}}
    pod_id = os.environ.get("RUNPOD_POD_ID")
    if isinstance(pod_id, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", pod_id):
        marker["pod_id"] = pod_id
    _write(fd, "session.json", _json(marker).encode())


def _flock_reliable(fd):
    """Only kernel local filesystems establish cross-process flock exclusion.

    FUSE, network and unverified mounts cannot prove owner death. Mount identity
    is read from the kernel, never from a claim stored on the volume.
    """
    try:
        device = os.fstat(fd).st_dev
        major_minor = f"{os.major(device)}:{os.minor(device)}"
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            types = {line.split(" - ", 1)[1].split()[0] for line in stream
                     if line.split()[2] == major_minor}
        return bool(types) and types <= {"ext4", "xfs", "btrfs", "tmpfs", "ramfs", "overlay"}
    except (OSError, ValueError, IndexError):
        return False


def _owner_process_state(marker):
    """Return positive incarnation evidence; an unreadable pid proves nothing."""
    pod_id = marker.get("pod_id")
    current_pod = os.environ.get("RUNPOD_POD_ID")
    if (isinstance(pod_id, str) and isinstance(current_pod, str)
            and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", pod_id)
            and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", current_pod) and pod_id != current_pod):
        return "dead", "different_pod"
    process = marker.get("process")
    if not isinstance(process, dict) or set(process) != {"pid", "birth_id"}:
        return "unknown", "none"
    pid, birth = process["pid"], process["birth_id"]
    if (type(pid) is not int or pid <= 0 or not isinstance(birth, str)
            or re.fullmatch(r"[0-9a-f-]{36}:[0-9]+", birth) is None):
        return "unknown", "none"
    current = process_birth_identity()
    if not current:
        return "unknown", "none"
    if birth.split(":")[0] != current.split(":")[0]:
        return "dead", "boot_id"
    observed = process_birth_identity(pid) or process_birth_identity(pid)
    if observed:
        return ("alive" if observed == birth else "dead"), "pid_start_ticks"
    # The shared helper deliberately collapses absent, zombie and unreadable.
    # Only ENOENT or a witnessed zombie disambiguates death from failed reads.
    if _zombie_birth_identity(pid) == birth:
        return "dead", "pid_start_ticks"
    try:
        os.stat(f"/proc/{pid}")
    except FileNotFoundError:
        return "dead", "pid_start_ticks"
    except OSError:
        pass
    return "unknown", "none"


def _wait_owner_lock(control, deadline):
    """Wait for the inherited owner handle, even after marker retirement."""
    try:
        with _directory(control, create=False) as fd:
            lock = os.open("owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                info = os.fstat(lock)
                _private(info, stat.S_ISREG)
                if info.st_nlink != 1:
                    raise UnsafePathError("unsafe engine owner lock")
                while True:
                    try:
                        native.lock_file(lock, blocking=False)
                        break
                    except BlockingIOError:
                        time.sleep(min(0.02, _remaining(deadline)))
            finally:
                os.close(lock)
    except FileNotFoundError:
        pass


def _engine_queue(root, engine, platform, deadline):
    """Read the configured loopback queue directly, without proxies or redirects."""
    from ._engine_target_files import _TargetFiles

    path = str(Path(root, "runtime/out/sessions", engine, "config.json"))
    queue_deadline = min(deadline, time.monotonic() + 2)
    try:
        files = _TargetFiles(root)
        if files.file_fact(path, hash_bytes=False, deadline=queue_deadline).exists:
            config = files.read_json(path, deadline=queue_deadline)
            port = config["port"]
        else:
            port = recipes.load_recipe(engine, platform).start.port
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError("invalid engine queue port")
        queue = _get_json(port, "/queue", _OUTPUT_LIMIT, queue_deadline,
                          lambda: _remaining(queue_deadline))
        running, pending = queue["queue_running"], queue["queue_pending"]
        if not isinstance(running, list) or not isinstance(pending, list):
            raise ValueError("invalid engine queue")
        return running, pending
    except (OSError, ValueError, KeyError, TypeError, RecursionError, EngineError) as exc:
        raise CustodyUnresolved("Cannot inspect the local engine queue; leave its supervisor running "
                               "and restore its control connection before retrying installation") from exc


def _busy_message(running, pending):
    def names(items):
        return ", ".join(str(item[1] if isinstance(item, list) and len(item) > 1 else item)
                         for item in items)[:600]
    work = []
    if running:
        work.append("running: " + names(running))
    if pending:
        work.append("pending: " + names(pending))
    return "Engine has running or pending work (" + "; ".join(work) + ")"


def refuse_busy_engine(*, root, engine, platform, deadline):
    """A job owns the install lock; an empty queue does not end its custody."""
    from .engine_contracts import EngineBusy

    counts = None
    try:
        running, pending = _engine_queue(root, engine, platform, deadline)
        counts = len(running), len(pending)
    except CustodyUnresolved as exc:
        if Path(root, ".astrid-engine/session/session.json").exists():
            client = LinuxEngineServiceClient.for_stop(root=root, engine=engine, platform=platform)
            if client is not None:
                with _directory(client.control, create=False) as fd:
                    raise client._owner_refusal(fd, CustodyUnresolved, "owner_unreachable") from exc
            raise
        running, pending = [], []
    message = (_busy_message(running, pending) if running or pending else
               "The local engine is held by a running or preparing job; retry installation when it finishes")
    client = LinuxEngineServiceClient(root=root, engine=engine, platform=platform,
                                     recipe_sha256="0" * 64, helper_sha256="0" * 64)
    try:
        with _directory(client.control, create=False) as fd:
            error = client._owner_refusal(fd, EngineBusy, "owner_busy")
    except (EngineError, OSError):
        # A held job lock is already sufficient for refusal. Marker diagnostics
        # may be unavailable; they cannot turn that refusal into admission.
        error = client._refusal(EngineBusy, None, reason="owner_busy")
    error.args = (str(error) + " " + message,)
    if counts is not None:
        error.running_count, error.pending_count = counts
    raise error


def stop_engine_for_converge(plan, *, context, deadline):
    """Under the exclusive install lock, refuse queued work before stopping."""
    if context.placement != "local":
        return
    client = LinuxEngineServiceClient.for_stop(root=plan.root, engine=plan.engine, platform=plan.platform)
    if client is None:
        control = str(Path(plan.root, ".astrid-engine/session"))
        try:
            _wait_owner_lock(control, deadline)
        except DeadlineExceeded as exc:
            diagnostic = LinuxEngineServiceClient(root=plan.root, engine=plan.engine, platform=plan.platform,
                                                   recipe_sha256="0" * 64, helper_sha256="0" * 64)
            raise diagnostic._refusal(CustodyUnresolved, None, reason="owner_lock_timeout",
                                      lock_state="held") from exc
        return
    with _directory(client.control, create=False) as fd:
        if client._retire_dead_owner(fd, deadline):
            return
    try:
        running, pending = _engine_queue(plan.root, plan.engine, plan.platform, deadline)
    except CustodyUnresolved as exc:
        with _directory(client.control, create=False) as fd:
            raise client._owner_refusal(fd, CustodyUnresolved, "owner_unreachable") from exc
    if running or pending:
        from .engine_contracts import EngineBusy
        with _directory(client.control, create=False) as fd:
            error = client._owner_refusal(fd, EngineBusy, "owner_busy")
        error.args = (str(error) + " " + _busy_message(running, pending),)
        error.running_count, error.pending_count = len(running), len(pending)
        raise error
    client.stop_owned(deadline=deadline)


def engine_log_path(root: str, engine: str) -> str:
    """The engine's complete stdout and stderr, appended from every launch at this root."""
    return root + "/runtime/out/sessions/" + engine + "-logs/engine.log"


def _engine_log_size(path: str) -> int:
    try:
        info = os.stat(path)
        return info.st_size if stat.S_ISREG(info.st_mode) else 0
    except OSError:
        return 0


def _write_engine_launch(path: str, launch: int, ready: int | None) -> None:
    target = Path(os.path.dirname(path), "launch.json")
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".launch.", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"schema": "engine-launch.v1", "launch": launch, "ready": ready}, stream,
                      sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def declare_engine_log(out, root: str, engine: str) -> None:
    """Declare the slice of the engine log that belongs to the run writing ``out``.

    Called before the run prepares the engine: the slice starts at the log's size
    now, so a freshly started engine's startup output belongs to this run and a
    reused engine's earlier output does not. Never raises: declaring logs can
    never fail or alter a job.
    """
    try:
        from astrid.core._shared import run_logs

        log = engine_log_path(str(root), engine)
        start = _engine_log_size(log)
        run_logs.declare(out, run_logs.LogDecl("engine", "engine", log, start=start))
        try:
            value = json.loads(Path(os.path.dirname(log), "launch.json").read_text(encoding="utf-8"))
            launch, ready = value.get("launch"), value.get("ready")
            if not isinstance(value, dict) or value.get("schema") != "engine-launch.v1":
                raise ValueError("invalid engine launch metadata")
            valid_launch = type(launch) is int and launch >= 0
            valid_ready = ready is None or type(ready) is int and launch <= ready
            if valid_launch and valid_ready and launch < start:
                end = ready if ready is not None and ready <= start else start
                run_logs.declare(out, run_logs.LogDecl("engine.startup", "engine", log,
                                                       start=launch, end=end))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    except Exception:  # noqa: BLE001 - log declaration must not affect the job
        pass


class _Output:
    """Drain one child pipe into a bounded ring and, when given, a complete append-only log."""

    def __init__(self, stream, limit, log_path=None):
        self.stream, self.limit = stream, limit
        self.data, self.total = bytearray(), 0
        self.lock = threading.Lock()
        self.sink = None
        self.ended = False
        if log_path is not None:
            self.attach_sink(log_path)
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def attach_sink(self, path):
        with self.lock:
            if self.ended or self.sink is not None:
                return
            try:
                self.sink = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
            except OSError:
                pass

    def _drain(self):
        try:
            while block := self.stream.read(65536):
                with self.lock:
                    self.total += len(block)
                    self.data.extend(block)
                    del self.data[:max(0, len(self.data) - self.limit)]
                    if self.sink is not None:
                        try:
                            view = memoryview(block)
                            while view:
                                written = os.write(self.sink, view)
                                if not written:
                                    raise OSError("sink write made no progress")
                                view = view[written:]
                        except OSError:
                            try:
                                os.close(self.sink)
                            except OSError:
                                pass
                            self.sink = None
        finally:
            with self.lock:
                self.ended = True
                if self.sink is not None:
                    try:
                        os.close(self.sink)
                    except OSError:
                        pass
                    self.sink = None

    def text(self):
        encoded = bytes(self.data).decode("utf-8", errors="replace").encode("utf-8")
        return encoded[-self.limit:].decode("utf-8", errors="ignore") if self.limit else ""

    def truncated(self):
        return self.total > self.limit or len(bytes(self.data).decode("utf-8", errors="replace").encode()) > self.limit


class _Owned:
    def __init__(self, request, *, secrets=(), output_log=None):
        from astrid.core.execution.process_group import popen_owned_group
        # The caller's library path is never inherited; request.env (built by
        # engine_child_env) is the only source.
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
        for key, value in request.env.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        self.cleanup_failure = None
        self._cleanup_secrets = tuple(secrets) + tuple(v for k, v in env.items() if _SECRET_KEY.search(k))
        for index, arg in enumerate(request.argv):
            if arg.startswith("-") and _SECRET_KEY.search(arg.partition("=")[0]):
                if "=" in arg:
                    self._cleanup_secrets += (arg.partition("=")[2],)
                elif index + 1 < len(request.argv):
                    self._cleanup_secrets += (request.argv[index + 1],)
        self.process = None
        try:
            self.process = popen_owned_group(list(request.argv), cwd=request.cwd, env=env,
                                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, bufsize=0, _delegate_inventory=False)
        except BaseException as exc:
            self.process = getattr(exc, "process", None)
            raise
        self.stdout = _Output(self.process.stdout, request.max_output_bytes, output_log)
        self.stderr = _Output(self.process.stderr, request.max_output_bytes, output_log)

    def live(self):
        if getattr(self.process, "_astrid_tree_uncertain", None):
            raise CustodyUnresolved("native engine custody unresolved")
        return not native.tree_exited(self.process)

    def stop(self, deadline):
        from astrid.core.execution.process_group import terminate_tree, verify_tree_absent
        stage = "terminate"
        try:
            terminate_tree(self.process, grace_seconds=0.1, cleanup_deadline=deadline)
            stage = "verify"
            verify_tree_absent(self.process, deadline=deadline)
            for name in ("stdout", "stderr"):
                output = getattr(self, name, None)
                if output is None:
                    continue
                stage = name + "_drain"
                output.thread.join(timeout=max(0, deadline - time.monotonic()))
                if output.thread.is_alive():
                    raise CustodyUnresolved("owned output drain unresolved")
                stage = name + "_close"
                output.stream.close()
        except BaseException as exc:
            if self.cleanup_failure is None:
                self.cleanup_failure = _cleanup_failure(stage, exc, self._cleanup_secrets)
            cause = self.cleanup_failure
            error = CustodyUnresolved('native owned process cleanup unresolved')
            error.cleanup_failure = cause
            raise error from exc


def _command(value, deadline):
    if (not isinstance(value, dict) or set(value) != {"argv", "cwd", "env", "max_output_bytes"}
            or not isinstance(value["argv"], list) or not value["argv"]
            or any(not isinstance(a, str) or "\0" in a for a in value["argv"])
            or not isinstance(value["cwd"], str) or not value["cwd"].startswith("/")
            or "\0" in value["cwd"]
            or not isinstance(value["env"], dict)
            or any(not isinstance(k, str) or not k or "=" in k or "\0" in k
                   or v is not None and (not isinstance(v, str) or "\0" in v)
                   for k, v in value["env"].items())
            or type(value["max_output_bytes"]) is not int
            or not 0 <= value["max_output_bytes"] <= _OUTPUT_LIMIT):
        raise RecipeValidationError("invalid bounded command")
    return CommandRequest(tuple(value["argv"]), value["cwd"], value["env"], deadline, value["max_output_bytes"])


def _vibe_birth(pid):
    # VibeComfy's Linux session registry uses /proc start ticks (native custody
    # independently retains boot+ticks and pidfds). This is observation only.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def _listener_owned(process, port):
    inodes = set()
    for name in ("tcp", "tcp6"):
        try:
            rows = Path("/proc/net/" + name).read_text().splitlines()[1:]
        except FileNotFoundError:
            if name == "tcp6":  # Kernel with IPv6 disabled.
                continue
            raise
        for row in rows:
            fields = row.split()
            address, port_hex = fields[1].split(":")
            if int(port_hex, 16) == port and fields[3] == "0A":
                if name != "tcp" or address != "0100007F":
                    return False
                inodes.add(fields[9])
    held = set()
    for entry in Path(f"/proc/{process.pid}/fd").iterdir():
        try:
            target = os.readlink(entry)
        except FileNotFoundError:
            continue
        if target.startswith("socket:["):
            held.add(target[8:-1])
    return len(inodes) == 1 and inodes <= held


def _verify_connected_child(channel, owned, port, deadline, check):
    """Prove the connected peer socket belongs to the retained child before sending bytes."""
    local_port = channel.getsockname()[1]
    local = f"0100007F:{port:04X}"
    remote = f"0100007F:{local_port:04X}"
    while True:
        check()
        if not owned.live() or not _listener_owned(owned.process, port):
            raise EndpointOwnershipError("engine listener ownership changed")
        inodes = set()
        for row in Path("/proc/net/tcp").read_text().splitlines()[1:]:
            fields = row.split()
            if fields[1] == local and fields[2] == remote and fields[3] == "01":
                inodes.add(fields[9])
        held = set()
        for entry in Path(f"/proc/{owned.process.pid}/fd").iterdir():
            try:
                target = os.readlink(entry)
            except FileNotFoundError:
                continue
            if target.startswith("socket:["):
                held.add(target[8:-1])
        if len(inodes) == 1 and inodes <= held and owned.live():
            return
        time.sleep(min(0.01, _remaining(deadline)))


def _get_json(port, path, limit, deadline, check, *, owned=None, payload=None):
    """Bound the whole HTTP exchange, including slow headers and body reads."""
    with socket.socket() as channel:
        channel.settimeout(min(1.0, _remaining(deadline)))
        channel.connect(("127.0.0.1", port))
        if owned is not None:
            _verify_connected_child(channel, owned, port, deadline, check)
        body = b"" if payload is None else _json(payload).encode()
        method = "GET" if payload is None else "POST"
        headers = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n"
        if payload is not None:
            headers += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        check()
        channel.sendall(headers.encode("ascii") + b"\r\n" + body)
        data = bytearray()
        def receive():
            while True:
                check()
                channel.settimeout(min(0.2, _remaining(deadline)))
                try:
                    block = channel.recv(65536)
                except socket.timeout:
                    continue
                if not block:
                    raise ValueError("short engine HTTP response")
                return block
        while b"\r\n\r\n" not in data:
            data.extend(receive())
            if len(data.split(b"\r\n\r\n", 1)[0]) > 65536:
                raise ValueError("engine HTTP headers exceed limit")
        raw, body = bytes(data).split(b"\r\n\r\n", 1)
        lines = raw.decode("ascii").split("\r\n")
        if lines[0].split(" ")[:2] not in (["HTTP/1.1", "200"], ["HTTP/1.0", "200"]):
            raise ValueError("engine HTTP status is not 200")
        headers = {}
        for line in lines[1:]:
            key, value = line.split(":", 1)
            key = key.lower()
            if key in headers or key.strip() != key:
                raise ValueError("ambiguous engine HTTP headers")
            headers[key] = value.strip()
        length = headers.get("content-length", "")
        if (not length.isascii() or not length.isdigit() or not 0 < int(length) <= limit
                or "transfer-encoding" in headers or "content-encoding" in headers):
            raise ValueError("unsupported engine HTTP framing")
        length = int(length)
        body = bytearray(body)
        while len(body) < length:
            body.extend(receive())
        if len(body) != length:
            raise ValueError("oversized engine HTTP body")
        value = _decode(bytes(body).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("engine health must be a JSON object")
        return value


def _installed_models_root(root, recipe):
    """The model directory the installed receipt bound; None (the default) when it records none."""
    from .engine_runner import _models_root
    try:
        raw = json.loads(Path(root, ".astrid-engine", "install.json").read_bytes())
    except (OSError, ValueError):
        return None
    value = raw.get("models_root") if isinstance(raw, dict) else None
    if value is None:
        return None
    return _models_root(root, value)


class _Supervisor:
    def __init__(self, identity, recipe, token):
        self.identity, self.recipe, self.token = identity, recipe, token
        self.root = identity["root"]
        self.control = self.root + "/.astrid-engine/session"
        self.adapter = LinuxPlatformAdapter(identity["platform"])
        self.launch = self.base_launch = None
        self.models_root = None
        self.engine = None
        self.commands = []  # Retain uncertain handles; never reconstruct them from disk.
        self.stopping = threading.Event()
        self.finished = threading.Event()
        self.finished_deadline = None
        self.stop_deadlines = {}
        self.stop_lock = threading.Lock()
        self.unresolved = False
        self.cleanup_failure = None
        self.work = threading.Lock()
        self.registry = self.root + "/runtime/out/sessions/" + identity["engine"]
        self.registry_names = []

    def check(self, channel, deadline):
        _remaining(deadline)
        if self.stopping.is_set():
            raise DeadlineExceeded("engine session stopping")
        if select.select([channel], [], [], 0)[0] and not channel.recv(1, socket.MSG_PEEK):
            raise EOFError

    def child_request(self, request):
        """Every engine subprocess (install helper, readiness probe, launch) gets the
        engine-built environment. Applied at spawn, so the launch fingerprint (the
        recipe env) never depends on the caller's or install-time library paths."""
        primary = next(e for e in self.recipe.environments if e.name == self.recipe.primary_environment)
        venv = self.adapter.join(self.root, primary.relative_path)
        return replace(request, env=engine_child_env(venv, request.env))

    def spawn(self, request, *, output_log=None):
        if self.unresolved:
            raise CustodyUnresolved("session retains an unresolved native cleanup obligation")
        # Retain even a failed-launch handle before propagating its error.
        owned = _Owned.__new__(_Owned)
        self.commands.append(owned)
        try:
            owned.__init__(self.child_request(request), secrets=(self.token,), output_log=output_log)
        except BaseException as exc:
            if getattr(owned, "process", None) is not None:
                self.unresolved = True
                raise CustodyUnresolved("owned process launch unresolved") from exc
            self.commands.remove(owned)
            if isinstance(exc, OSError):
                raise EndpointHealthError("owned executable could not start") from exc
            raise
        return owned

    def drain(self, owned, deadline):
        try:
            owned.stop(deadline)
        except BaseException as exc:
            self.unresolved = True
            if self.cleanup_failure is None:
                self.cleanup_failure = getattr(owned, "cleanup_failure", None) or _cleanup_failure("drain", exc, (self.token,))
                try:
                    with _directory(self.control) as fd:
                        _write(fd, "cleanup-failure.json", _json(self.cleanup_failure).encode())
                except (OSError, EngineError):
                    pass  # The in-memory first cause still survives a persistence failure.
            raise
        self.commands.remove(owned)

    def cleanup_deadline(self, deadline=None):
        # An explicit stop also owns cleanup in an interrupted run/start thread.
        with self.stop_lock:
            if self.stop_deadlines:
                return min(self.stop_deadlines.values())
        return deadline if deadline is not None else time.monotonic() + _CLEANUP_SECONDS

    def run(self, value, deadline, channel):
        request = _command(value, deadline)
        started = time.monotonic()
        owned = self.spawn(request)
        timed_out = False
        cleanup_deadline = deadline
        try:
            while owned.live():
                try:
                    self.check(channel, deadline)
                except DeadlineExceeded:
                    timed_out = True
                    cleanup_deadline = None
                    break
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        except BaseException:
            cleanup_deadline = None
            raise
        finally:
            self.drain(owned, self.cleanup_deadline(cleanup_deadline))
        return asdict(CommandResult(None if timed_out else owned.process.returncode,
                      owned.stdout.text(), owned.stderr.text(), timed_out,
                      time.monotonic() - started, uuid.uuid4().hex,
                      owned.stdout.truncated(), owned.stderr.truncated()))

    def facts(self, state, *, healthy=False, digest=None):
        return {"state": state, **self.identity, "launch_sha256": _sha(self.launch) if state == "owned" else None,
                "owner_id": self.token if state == "owned" else None, "healthy": healthy,
                "object_info_sha256": digest, "required_nodes_present": healthy}

    def health(self, channel, deadline):
        health = self.recipe.health
        deadline = min(deadline, time.monotonic() + health.timeout_seconds)
        while True:
            self.check(channel, deadline)
            if not self.engine.live():
                raise EndpointHealthError("owned engine exited before readiness")
            try:
                if not _listener_owned(self.engine.process, self.recipe.start.port):
                    raise ValueError("listener does not belong to the owned engine")
                check = lambda: self.check(channel, deadline)
                _get_json(self.recipe.start.port, health.path, 1024 * 1024, deadline, check)
                nodes = _get_json(self.recipe.start.port, health.object_info_path, 32 * 1024 * 1024, deadline, check)
                if not set(health.required_nodes) <= nodes.keys():
                    raise ValueError("required engine nodes missing")
                check()
                if not self.engine.live() or not _listener_owned(self.engine.process, self.recipe.start.port):
                    raise EndpointHealthError("engine ownership changed during health")
                return self.facts("owned", healthy=True, digest=_object_info_sha256(nodes))
            except (OSError, ValueError):
                time.sleep(min(health.poll_interval_seconds, _remaining(deadline)))

    def clear_registry(self):
        if self.registry_names:
            with _directory(self.registry, create=False) as fd:
                for name in self.registry_names:
                    try:
                        os.unlink(name, dir_fd=fd)
                    except FileNotFoundError:
                        pass
            self.registry_names.clear()

    def stop_engine(self, deadline=None):
        deadline = self.cleanup_deadline(deadline)
        if self.engine is not None:
            self.drain(self.engine, deadline)
            self.engine = None
        self.clear_registry()

    def source_identity(self, deadline, channel):
        """Use VibeComfy's exact effective-source hash convention, with owned git."""
        source = next((s for s in self.recipe.sources if s.name == "vibecomfy"), None)
        if source is None:
            raise RecipeValidationError(
                'comfyui recipe must bind a VibeComfy source at source_bindings',
                step_id="source:vibecomfy",
            )
        root = Path(self.root, source.relative_path)
        env = {k: None for k in os.environ if k.startswith("GIT_")}
        def git(argv):
            result = self.run(dict(argv=["git", *argv], cwd=str(root), env=env,
                                   max_output_bytes=_OUTPUT_LIMIT), deadline, channel)
            if result["timed_out"]:
                raise DeadlineExceeded("source identity deadline exceeded")
            if result["exit_code"] != 0 or result["stdout_truncated"]:
                raise IntegrityError("VibeComfy source identity unavailable")
            return result["stdout"]
        revision = git(["rev-parse", "HEAD"]).strip()
        if revision != source.pin.revision:
            raise IntegrityError("VibeComfy source revision changed")
        digest = hashlib.sha256()
        for relative in git(["ls-files", "-z", "-c", "-o", "--exclude-standard"]).split("\0"):
            self.check(channel, deadline)
            if not relative or relative.endswith(".pyc") or "__pycache__" in Path(relative).parts:
                continue
            path = root / relative
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                raise IntegrityError("unsafe VibeComfy source identity member")
            content = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    self.check(channel, deadline)
                    content.update(block)
            digest.update(os.fsencode(relative) + b"\0" + content.digest())
        return revision, "sha256:" + digest.hexdigest()

    def publish_registry(self, source):
        request, port = self.launch, self.recipe.start.port
        models_root = self.models_root
        output = request["cwd"] + "/output"
        if "--output-directory" in request["argv"]:
            output = request["argv"][request["argv"].index("--output-directory") + 1]
        config = {"runtime_root": self.root + "/runtime", "cwd": request["cwd"], "port": port,
                  "warm_policy": "never", "base_directory": request["cwd"],
                  "models_root": models_root, "models_root_normalized": models_root,
                  "output_directory": output, "locality": "managed_local_server",
                  "server_log_path": self.registry + "/comfy.log"}
        url = f"http://127.0.0.1:{port}"
        marker = {"pid": os.getpid(), "process_start_identity": _vibe_birth(os.getpid()),
                  "comfy_pid": self.engine.process.pid,
                  "comfy_process_start_identity": _vibe_birth(self.engine.process.pid),
                  "url": url, "launch_token": self.token}
        values = {"config.json": _json(config), "pid": str(os.getpid()), "comfy_pid": str(marker["comfy_pid"]),
                  "comfy_process_start_identity": marker["comfy_process_start_identity"], "url": url,
                  "source_revision": source[0], "source_content_digest": source[1],
                  "daemon.log": "", "comfy.log": self.engine.stdout.text() + self.engine.stderr.text(),
                  "launch.json": _json(marker)}
        with _directory(self.registry) as fd:
            if os.listdir(fd):
                raise CustodyUnresolved("existing VibeComfy registry cannot be adopted")
            for name, value in values.items():
                self.registry_names.append(name)
                _write(fd, name, value.encode())

    def valid_launch(self, launch):
        if not isinstance(launch, dict) or set(launch) != {"argv", "cwd", "env"}:
            return False
        env = launch.get("env")
        if not isinstance(env, dict) or "CUDA_VISIBLE_DEVICES" not in env:
            return False
        device = env.get("CUDA_VISIBLE_DEVICES")
        if device is not None and (not isinstance(device, str) or "\0" in device
                                   or len(device.encode("utf-8")) > 4096):
            return False
        expected = dict(self.base_launch["env"])
        if "CUDA_VISIBLE_DEVICES" in env:
            expected["CUDA_VISIBLE_DEVICES"] = device
        return launch["argv"] == self.base_launch["argv"] and launch["cwd"] == self.base_launch["cwd"] and env == expected

    def service(self, operation, payload, deadline, channel):
        if self.unresolved:
            raise CustodyUnresolved("session retains an unresolved native cleanup obligation")
        if self.engine is None:
            # The first install helper creates this supervisor before install.json
            # exists. Bind at admission, once the runner has declared its model
            # directory, and retain that same binding for the child and registry.
            self.models_root = effective_models_root(self.root, _installed_models_root(self.root, self.recipe))
            request = self.adapter.start_request(self.recipe, self.root, deadline=deadline,
                                                 models_root=self.models_root)
            self.base_launch = {"argv": list(request.argv), "cwd": request.cwd, "env": dict(request.env)}
        expected = {"schema", *self.identity, "launch", "owner_id", "timeout_seconds", "poll_interval_seconds"}
        if (not isinstance(payload, dict) or set(payload) != expected or payload["schema"] != _SCHEMA
                or any(payload[k] != v for k, v in self.identity.items())
                or not self.valid_launch(payload["launch"])
                or self.engine is not None and payload["launch"] != self.launch
                or payload["owner_id"] not in (None, self.token)
                or payload["timeout_seconds"] != self.recipe.health.timeout_seconds
                or payload["poll_interval_seconds"] != self.recipe.health.poll_interval_seconds):
            raise EndpointOwnershipError("service request differs from bound recipe or owner")
        if self.engine is not None and not self.engine.live():
            self.stop_engine(deadline)
        if self.engine is None:
            # Availability is observational only. Never adopt or signal a port owner.
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", self.recipe.start.port))
                except OSError:
                    return self.facts("unowned")
            if operation != "start":
                return self.facts("absent")
            self.launch = payload["launch"]
            _TargetFiles(self.models_root).ensure_dir(self.models_root, deadline=deadline)
            source = self.source_identity(deadline, channel)
            path = engine_log_path(self.root, self.identity["engine"])
            try:
                os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            except OSError:
                pass
            request = _command({**self.launch, "max_output_bytes": 262144}, deadline)
            # The engine's complete stdout and stderr are appended here from launch.
            launch_size = _engine_log_size(path)
            self.engine = self.spawn(request, output_log=path)
            ready = None
            try:
                facts = self.health(channel, deadline)
                ready = _engine_log_size(path)
                _write_engine_launch(path, launch_size, ready)
                self.publish_registry(source)
                self.check(channel, deadline)
                return facts
            except BaseException:
                if ready is None:
                    _write_engine_launch(path, launch_size, None)
                self.stop_engine()
                raise
        if operation == "health":
            return self.health(channel, deadline)
        return self.facts("owned")

    def release(self, payload, deadline, channel):
        if payload != {} or self.unresolved or self.engine is None:
            raise EndpointOwnershipError("live engine custody is required")
        engine = self.engine
        check = lambda: self.check(channel, deadline)
        def request(path, payload=None):
            return _get_json(self.recipe.start.port, path, 1024 * 1024, deadline, check,
                             owned=engine, payload=payload)
        def vram(stats):
            devices = stats.get("devices")
            if not isinstance(devices, list) or not devices or not isinstance(devices[0], dict):
                raise ValueError("managed ComfyUI reported no device VRAM")
            total, free = devices[0].get("vram_total"), devices[0].get("vram_free")
            if type(total) is not int or type(free) is not int:
                raise ValueError("managed ComfyUI reported malformed VRAM")
            return total, free
        total, before = vram(request("/system_stats"))
        queue = request("/queue")
        running, pending = queue.get("queue_running"), queue.get("queue_pending")
        if not isinstance(running, list) or not isinstance(pending, list):
            raise ValueError("managed ComfyUI queue state is malformed")
        if running or pending:
            return {"status": "busy"}
        try:
            free = request("/api/free", {"unload_models": True, "free_memory": True})
            unloaded = free.get("models_unloaded") is True
        except (OSError, ValueError):
            unloaded = False
        _, after = vram(request("/system_stats"))
        result = {"status": "completed" if unloaded else "failed", "models_unloaded": unloaded,
                  "vram_total_bytes": total, "vram_free_before_bytes": before,
                  "vram_free_after_bytes": after}
        # The same append-only engine log is declared by the preparing run.
        # Retain the native release evidence even when idle fires after settle.
        try:
            with open(engine_log_path(self.root, self.identity["engine"]), "a", encoding="utf-8") as log:
                log.write(json.dumps(result, sort_keys=True) + "\n")
        except OSError:
            pass  # Logging cannot change the release outcome.
        return result

    def stop(self, deadline=None):
        deadline = self.cleanup_deadline(deadline)
        for owned in list(self.commands):
            if owned is not self.engine:
                self.drain(owned, deadline)
        # Retire engine/registry only after every other owned drain is certified.
        self.stop_engine(deadline)

    def connection(self, channel):
        operation = None
        try:
            value = _receive(channel, time.monotonic() + 5)
            if (not isinstance(value, dict) or set(value) != {"identity", "operation", "payload", "remaining_seconds"}
                    or value["identity"] != self.identity):
                raise EndpointOwnershipError("supervisor installation identity differs")
            remaining = value["remaining_seconds"]
            if type(remaining) not in (int, float) or not math.isfinite(remaining) or not 0 < remaining <= 604800:
                raise RecipeValidationError("invalid supervisor operation budget")
            deadline = time.monotonic() + remaining
            operation = value["operation"]
            if operation == "stop":
                with self.stop_lock:
                    self.stop_deadlines[channel] = deadline
                    self.stopping.set()
            response_deadline = deadline if operation == "stop" else deadline + _CLEANUP_SECONDS
            if not self.work.acquire(timeout=_remaining(response_deadline)):
                raise CustodyUnresolved("active supervisor operation did not drain")
            engine_before = self.engine
            try:
                if operation == "stop":
                    self.stop(deadline)
                    result = {"stopped": True}
                    self.finished_deadline = deadline
                    self.finished.set()
                else:
                    self.check(channel, deadline)
                    if operation == "run":
                        result = self.run(value["payload"], deadline, channel)
                    elif operation == "release":
                        result = self.release(value["payload"], deadline, channel)
                    elif operation in {"probe", "start", "health"}:
                        result = self.service(operation, value["payload"], deadline, channel)
                    else:
                        raise RecipeValidationError("unsupported supervisor operation")
                _send(channel, {"identity": self.identity, "result": result}, response_deadline)
            except (OSError, EOFError):
                if operation == "start" and self.engine is not engine_before:
                    self.stop_engine()
                raise
            finally:
                self.work.release()
        except BaseException as exc:
            try:
                response = {"identity": self.identity, "error": exc.code if isinstance(exc, EngineError)
                            else CustodyUnresolved.code,
                            "reason": (engine_reason(exc.reason) if isinstance(exc, EngineError) else None)
                            or "engine supervisor operation failed"}
                if self.cleanup_failure is not None:
                    response["cleanup_failure"] = self.cleanup_failure
                _send(channel, response, time.monotonic() + 1)
            except (OSError, EngineError):
                pass
        finally:
            if operation == "stop":
                # Failed stops retain this live owner and its marker for recovery.
                with self.stop_lock:
                    self.stop_deadlines.pop(channel, None)
                    if not self.finished.is_set() and not self.stop_deadlines:
                        self.stopping.clear()
            channel.close()


def _serve(identity, recipe, lock_fd, token):
    owner = _Supervisor(identity, recipe, token)
    signalled = threading.Event()
    def stopping(*_args):
        owner.stopping.set()
        signalled.set()
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    threads = []
    clean = False
    with _directory(owner.control, create=False) as fd:
        _publish_owner_marker(fd, identity, token, os.getpid())
        with socket.socket(socket.AF_UNIX) as listener:
            pending = "." + uuid.uuid4().hex + ".sock"
            previous_umask = os.umask(0o177)
            try:
                listener.bind(f"/proc/self/fd/{fd}/{pending}")
            finally:
                os.umask(previous_umask)
            try:
                listener.listen(8)
                # Clients may connect as soon as control.sock is visible.
                # Publish only a listening socket under the retained owner lock.
                os.link(pending, "control.sock", src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            finally:
                if _exists(fd, pending) is not None:
                    os.unlink(pending, dir_fd=fd)
            listener.settimeout(0.2)
            try:
                while not owner.finished.is_set():
                    if signalled.is_set() and owner.work.acquire(blocking=False):
                        try:
                            owner.stop()
                            owner.finished.set()
                        except CustodyUnresolved:
                            owner.stopping.clear()
                        finally:
                            signalled.clear()
                            owner.work.release()
                        continue
                    try:
                        channel, _ = listener.accept()
                    except socket.timeout:
                        continue
                    threads = [t for t in threads if t.is_alive()]
                    if len(threads) >= 8:
                        channel.close()
                        continue
                    thread = threading.Thread(target=owner.connection, args=(channel,), daemon=True)
                    threads.append(thread)
                    thread.start()
            finally:
                for thread in threads:
                    thread.join(timeout=_CLEANUP_SECONDS + 5)
                if not any(t.is_alive() for t in threads):
                    with owner.work:
                        owner.stop(owner.finished_deadline)
                        clean = True
                if clean:
                    os.unlink("control.sock", dir_fd=fd)
                    os.unlink("session.json", dir_fd=fd)
                    os.fsync(fd)
    os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("_serve", "stop"))
    parser.add_argument("value")
    parser.add_argument("--engine", default="comfyui")
    parser.add_argument("--platform", default="linux-x86_64-cu130")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--lock-fd", type=int)
    parser.add_argument("--launch-token")
    args = parser.parse_args()
    if args.operation == "stop":
        recipe = recipes.load_recipe(args.engine, args.platform)
        LinuxEngineServiceClient(root=args.value, engine=args.engine, platform=args.platform,
            recipe_sha256=recipes.recipe_digest(recipe), helper_sha256=_sha(helper_manifest(recipe))).stop_owned(
                deadline=time.monotonic() + args.timeout)
        return
    identity = _decode(args.value)
    recipe = recipes.load_recipe(identity["engine"], identity["platform"])
    if (identity["recipe_sha256"] != recipes.recipe_digest(recipe)
            or identity["helper_sha256"] != _sha(helper_manifest(recipe))):
        raise IntegrityError("installed supervisor code/recipe differs from caller")
    _serve(identity, recipe, args.lock_fd, args.launch_token)


if __name__ == "__main__":
    main()
