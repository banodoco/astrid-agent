"""Installation-scoped Linux owner for trusted engine and finite commands.

One private Unix socket; authority stays in the live owner's native handles.
The session marker fences owner loss. It never authorizes PID adoption.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
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
import threading
import time
import uuid

from runtime_protocol import platform as native

from . import engine_recipe as recipes
from ._engine_python_bootstrap import engine_child_env
from .engine_contracts import (
    CommandRequest, CommandResult, CustodyUnresolved, DeadlineExceeded,
    EndpointHealthError, EndpointOwnershipError, EngineError, IntegrityError,
    RecipeValidationError, UnsafePathError, UnsupportedPlatform,
)
from .engine_runner import _json, _root, _sha, helper_manifest
from .engine_platforms import LinuxPlatformAdapter

_SCHEMA = "astrid.engine-service.v1"
_FRAME_LIMIT = 16 * 1024 * 1024
_OUTPUT_LIMIT = 1024 * 1024
_CLEANUP_SECONDS = 8.0
_ERRORS = {e.code: e for e in (CustodyUnresolved, DeadlineExceeded, EndpointHealthError,
           EndpointOwnershipError, IntegrityError, RecipeValidationError, UnsafePathError)}


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


def _receive(channel, deadline):
    def read(size):
        data = bytearray()
        while len(data) < size:
            channel.settimeout(_remaining(deadline))
            block = channel.recv(min(65536, size - len(data)))
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

    def _connect(self, fd, deadline, *, start):
        while True:
            _remaining(deadline)
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
                    raise CustodyUnresolved("original supervisor unavailable; explicit recovery required") from None
                except BaseException:
                    channel.close()
                    raise
            if _exists(fd, "session.json") is not None:
                _private(_exists(fd, "session.json"), stat.S_ISREG)
                lock = os.open("owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                try:
                    try:
                        native.lock_file(lock, blocking=False)
                    except BlockingIOError:
                        pass
                    else:
                        raise CustodyUnresolved("original supervisor lost; explicit recovery required")
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
                _write(fd, "session.json", _json({"identity": self.identity, "owner_id": token}).encode())
                # The child inherits the locked open description. Closing the parent
                # copy (without LOCK_UN) leaves ownership with that live supervisor.
                self._spawned = subprocess.Popen(
                    _supervisor_argv(self.identity, lock, token), pass_fds=(lock,),
                    cwd=self.identity["root"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                )
            except BaseException as exc:
                if isinstance(exc, EngineError):
                    raise
                raise CustodyUnresolved("supervisor launch uncertain; explicit recovery required") from exc
            finally:
                os.close(lock)

    def _request(self, operation, payload, deadline, *, start=True):
        _remaining(deadline)
        try:
            with _directory(self.control, create=start) as fd:
                try:
                    channel = self._connect(fd, deadline, start=start)
                except DeadlineExceeded as exc:
                    if _exists(fd, "session.json") is not None:
                        raise CustodyUnresolved("supervisor unavailable; explicit recovery required") from exc
                    raise
                if channel is None:
                    return None
                with channel:
                    _send(channel, {"identity": self.identity, "operation": operation, "payload": payload,
                                    "remaining_seconds": _remaining(deadline)}, deadline)
                    # Keep the connection open while target deadline cleanup is verified.
                    response = _receive(channel, deadline + _CLEANUP_SECONDS)
                if (not isinstance(response, dict) or response.get("identity") != self.identity
                        or set(response) not in ({"identity", "result"}, {"identity", "error"})):
                    raise IntegrityError("unbound supervisor response")
                if "error" in response:
                    code = response["error"]
                    raise _ERRORS.get(code, CustodyUnresolved)("engine supervisor: " + str(code))
                return response["result"]
        except FileNotFoundError:
            if not start:
                return None
            raise UnsafePathError("engine control directory missing") from None
        except (OSError, EOFError, ValueError) as exc:
            raise CustodyUnresolved("supervisor response uncertain; do not replay") from exc

    def call(self, operation, payload, *, deadline):
        if operation not in {"probe", "start", "health"}:
            raise RecipeValidationError("unsupported engine service operation")
        return self._request(operation, payload, deadline)

    def run(self, request: CommandRequest) -> CommandResult:
        return CommandResult(**self._request("run", {"argv": list(request.argv), "cwd": request.cwd,
                             "env": dict(request.env), "max_output_bytes": request.max_output_bytes}, request.deadline))

    def stop_owned(self, *, deadline):
        self._request("stop", {}, deadline, start=False)
        # Successful explicit stop includes retirement of the ephemeral marker.
        while Path(self.control, "session.json").exists():
            time.sleep(min(0.02, _remaining(deadline + _CLEANUP_SECONDS)))
        if self._spawned is not None:
            self._spawned.wait(timeout=_remaining(deadline + _CLEANUP_SECONDS))


class _Output:
    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit
        self.data, self.total = bytearray(), 0
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        while block := self.stream.read(65536):
            self.total += len(block)
            self.data.extend(block)
            del self.data[:max(0, len(self.data) - self.limit)]

    def text(self):
        encoded = bytes(self.data).decode("utf-8", errors="replace").encode("utf-8")
        return encoded[-self.limit:].decode("utf-8", errors="ignore") if self.limit else ""

    def truncated(self):
        return self.total > self.limit or len(bytes(self.data).decode("utf-8", errors="replace").encode()) > self.limit


class _Owned:
    def __init__(self, request):
        from astrid.core.execution.process_group import popen_owned_group
        # The caller's library path is never inherited; request.env (built by
        # engine_child_env) is the only source.
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
        for key, value in request.env.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        self.process = None
        try:
            self.process = popen_owned_group(list(request.argv), cwd=request.cwd, env=env,
                                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, bufsize=0, _delegate_inventory=False)
        except BaseException as exc:
            self.process = getattr(exc, "process", None)
            raise
        self.stdout = _Output(self.process.stdout, request.max_output_bytes)
        self.stderr = _Output(self.process.stderr, request.max_output_bytes)

    def live(self):
        if getattr(self.process, "_astrid_tree_uncertain", None):
            raise CustodyUnresolved("native engine custody unresolved")
        return not native.tree_exited(self.process)

    def stop(self, deadline):
        from astrid.core.execution.process_group import _terminate_tree, verify_tree_absent
        try:
            _terminate_tree(self.process, grace_seconds=0.1, cleanup_deadline=deadline)
            verify_tree_absent(self.process)
            for output in (getattr(self, "stdout", None), getattr(self, "stderr", None)):
                if output is None:
                    continue
                output.thread.join(timeout=max(0, deadline - time.monotonic()))
                if output.thread.is_alive():
                    raise CustodyUnresolved("owned output drain unresolved")
                output.stream.close()
        except BaseException as exc:
            raise CustodyUnresolved("native owned process cleanup unresolved") from exc


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


def _get_json(port, path, limit, deadline, check):
    """Bound the whole HTTP exchange, including slow headers and body reads."""
    with socket.socket() as channel:
        channel.settimeout(min(1.0, _remaining(deadline)))
        channel.connect(("127.0.0.1", port))
        channel.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode("ascii"))
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


class _Supervisor:
    def __init__(self, identity, recipe, token):
        self.identity, self.recipe, self.token = identity, recipe, token
        self.root = identity["root"]
        self.control = self.root + "/.astrid-engine/session"
        self.adapter = LinuxPlatformAdapter(identity["platform"])
        request = self.adapter.start_request(recipe, self.root, deadline=time.monotonic() + 1)
        self.launch = {"argv": list(request.argv), "cwd": request.cwd, "env": dict(request.env)}
        self.engine = None
        self.commands = []  # Retain uncertain handles; never reconstruct them from disk.
        self.stopping = threading.Event()
        self.finished = threading.Event()
        self.unresolved = False
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

    def spawn(self, request):
        if self.unresolved:
            raise CustodyUnresolved("session retains an unresolved native cleanup obligation")
        # Retain even a failed-launch handle before propagating its error.
        owned = _Owned.__new__(_Owned)
        self.commands.append(owned)
        try:
            owned.__init__(self.child_request(request))
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
        except BaseException:
            self.unresolved = True
            raise
        self.commands.remove(owned)

    def run(self, value, deadline, channel):
        request = _command(value, deadline)
        started = time.monotonic()
        owned = self.spawn(request)
        timed_out = False
        try:
            while owned.live():
                try:
                    self.check(channel, deadline)
                except DeadlineExceeded:
                    timed_out = True
                    break
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        finally:
            self.drain(owned, time.monotonic() + _CLEANUP_SECONDS)
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
                return self.facts("owned", healthy=True, digest=_sha(nodes))
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

    def stop_engine(self):
        if self.engine is not None:
            self.drain(self.engine, time.monotonic() + _CLEANUP_SECONDS)
            self.engine = None
        self.clear_registry()

    def source_identity(self, deadline, channel):
        """Use VibeComfy's exact effective-source hash convention, with owned git."""
        source = next(s for s in self.recipe.sources if s.name == "vibecomfy")
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
        output = request["cwd"] + "/output"
        if "--output-directory" in request["argv"]:
            output = request["argv"][request["argv"].index("--output-directory") + 1]
        config = {"runtime_root": self.root + "/runtime", "cwd": request["cwd"], "port": port,
                  "warm_policy": "never", "base_directory": request["cwd"],
                  "models_root": self.root + "/models", "models_root_normalized": self.root + "/models",
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

    def service(self, operation, payload, deadline, channel):
        if self.unresolved:
            raise CustodyUnresolved("session retains an unresolved native cleanup obligation")
        expected = {"schema", *self.identity, "launch", "owner_id", "timeout_seconds", "poll_interval_seconds"}
        if (not isinstance(payload, dict) or set(payload) != expected or payload["schema"] != _SCHEMA
                or any(payload[k] != v for k, v in self.identity.items())
                or payload["launch"] != self.launch
                or payload["owner_id"] not in (None, self.token)
                or payload["timeout_seconds"] != self.recipe.health.timeout_seconds
                or payload["poll_interval_seconds"] != self.recipe.health.poll_interval_seconds):
            raise EndpointOwnershipError("service request differs from bound recipe or owner")
        if self.engine is not None and not self.engine.live():
            self.stop_engine()
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
            source = self.source_identity(deadline, channel)
            request = _command({**self.launch, "max_output_bytes": 262144}, deadline)
            self.engine = self.spawn(request)
            try:
                facts = self.health(channel, deadline)
                self.publish_registry(source)
                self.check(channel, deadline)
                return facts
            except BaseException:
                self.stop_engine()
                raise
        if operation == "health":
            return self.health(channel, deadline)
        return self.facts("owned")

    def stop(self):
        self.stop_engine()
        for owned in list(self.commands):
            self.drain(owned, time.monotonic() + _CLEANUP_SECONDS)

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
                self.stopping.set()
            if not self.work.acquire(timeout=_remaining(deadline + _CLEANUP_SECONDS)):
                raise CustodyUnresolved("active supervisor operation did not drain")
            engine_before = self.engine
            try:
                if operation == "stop":
                    self.stop()
                    result = {"stopped": True}
                    self.finished.set()
                else:
                    self.check(channel, deadline)
                    if operation == "run":
                        result = self.run(value["payload"], deadline, channel)
                    elif operation in {"probe", "start", "health"}:
                        result = self.service(operation, value["payload"], deadline, channel)
                    else:
                        raise RecipeValidationError("unsupported supervisor operation")
                _send(channel, {"identity": self.identity, "result": result}, deadline + _CLEANUP_SECONDS)
            except (OSError, EOFError):
                if operation == "start" and self.engine is not engine_before:
                    self.stop_engine()
                raise
            finally:
                self.work.release()
        except BaseException as exc:
            if operation == "stop" and not self.finished.is_set():
                # Keep this live owner and its handles available for explicit
                # recovery; a failed stop never retires its session marker.
                self.stopping.clear()
            try:
                _send(channel, {"identity": self.identity, "error": exc.code if isinstance(exc, EngineError)
                               else CustodyUnresolved.code}, time.monotonic() + 1)
            except (OSError, EngineError):
                pass
        finally:
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
        with socket.socket(socket.AF_UNIX) as listener:
            previous_umask = os.umask(0o177)
            try:
                listener.bind(_socket_path(fd))  # Private from its first visible instant.
            finally:
                os.umask(previous_umask)
            listener.listen(8)
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
                        owner.stop()
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
    parser.add_argument("--engine", default="vibecomfy-h3")
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
