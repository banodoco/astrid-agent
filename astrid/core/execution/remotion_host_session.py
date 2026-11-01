"""Private inherited renderer leases; original native custody stays in the host.

This channel is not a Runtime/capability bridge. Only the admitted visualize
executor inherits it, and the host fixes the executable and helper directory.
"""
from __future__ import annotations

import json
import hashlib
import shutil
import stat
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from astrid.core.subprocess_env import build_child_subprocess_env
from .pipe_io import PipeReader, PipeWriter, close_pipe_workers
from .process_group import popen_owned_group, observe_tree, verify_tree_absent, release_group, terminate_tree
from .warm_hold import WarmHolds, idle_seconds

_LIMIT = 8 * 1024 * 1024
_client = None


def frame_owner_idle_seconds(environment: Mapping[str, str] | None = None, *,
                             requested: float | None = None) -> float:
    """How long the Remotion frame owner stays warm after its last task.

    Its own default is 45 s, or 300 s with an explicit owner memory allocation
    (ASTRID_REMOTION_WARM_MEMORY_BUDGET_BYTES of at least 512 MiB).  The
    warm-hold policy (a request, then ASTRID_WARM_IDLE_SECONDS) overrides it.
    """
    environment = os.environ if environment is None else environment
    try:
        budget = int(environment.get("ASTRID_REMOTION_WARM_MEMORY_BUDGET_BYTES", "0"))
    except ValueError:
        budget = 0
    return idle_seconds(300.0 if budget >= 512 * 1024 ** 2 else 45.0, requested=requested)


def _read(channel):
    data = bytearray()
    while b"\n" not in data:
        part = channel.recv(min(65536, _LIMIT + 1 - len(data)))
        if not part:
            raise EOFError("renderer lease channel closed")
        data.extend(part)
        if len(data) > _LIMIT:
            raise ValueError("renderer lease frame exceeds bound")
    value = json.loads(data.split(b"\n", 1)[0])
    if not isinstance(value, dict):
        raise ValueError("renderer lease frame must be an object")
    return value


def _write(channel, value):
    encoded = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > _LIMIT:
        raise ValueError("renderer lease frame exceeds bound")
    channel.sendall(encoded)


class _PipeReader:
    """The existing response parser's recv interface over the original pipe."""
    def __init__(self, stream, timeout=300.0, guard=lambda: None):
        self.stream, self.deadline, self.guard = stream, time.monotonic() + timeout, guard

    def recv(self, count):
        try:
            return self.stream.read(count, deadline=self.deadline, guard=self.guard, poll_interval=0.1)
        except TimeoutError as error:
            raise TimeoutError("original Remotion worker response timed out") from error


def _file_identity(path):
    st = Path(path).stat()
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("selected renderer input is not a regular file")
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            hashlib.sha256(Path(path).read_bytes()).hexdigest())


class HostRemotionSession:
    """One host-owned original Node child, shared by sequential task leases."""
    def __init__(self, *, track, untrack, root_parent=None, account=lambda root: None, retire=lambda root: None,
                 report=lambda note: None, holds=None):
        self._lock = threading.RLock()
        self._track, self._untrack = track, untrack
        self._root_parent = root_parent
        self._account, self._retire, self._report = account, retire, report
        self._root_identity = None
        self.root = None
        self.process = None
        self.identity = None
        self.uncertain = None
        self._closed = False
        self._leases = set()
        self._holds = holds if holds is not None else WarmHolds()
        self._stderr = bytearray()
        self._stdout_reader = self._stderr_reader = self._stdin_writer = None
        self._idle_seconds = 45.0

    def lease(self, environment, *, attempt, requested_idle=None):
        with self._lock:
            if self._closed:
                raise RuntimeError("host Remotion owner is closed")
            if self.uncertain:
                # An uncertain child never blocks the host: retire it and start
                # fresh (new scratch root) on the next request.
                self._retire_uncertain()
            project = Path(environment.get("ASTRID_REMOTION_PROJECT_DIR", ""))
            node = Path(environment.get("ASTRID_NODE_EXECUTABLE", ""))
            if not project.is_absolute() or project.resolve() != project or not node.is_absolute():
                raise ValueError("host renderer lease requires qualified explicit project and Node")
            node = node.resolve(strict=True)
            if not (project / "src/astrid-frame-worker.mjs").is_file() or not node.is_file():
                raise ValueError("host renderer lease selected inputs are missing")
            cli = (project / "node_modules/@remotion/cli/remotion-cli.js").resolve(strict=True)
            if not cli.is_relative_to(project) or not cli.is_file():
                raise ValueError("host selected Remotion CLI escapes project")
            attempt = Path(attempt).resolve(strict=True)
            if not attempt.is_dir():
                raise ValueError("renderer task root is unavailable")
            self._idle_seconds = frame_owner_idle_seconds(environment, requested=requested_idle)
            lease_environment = dict(environment)
            if "HOME" not in lease_environment and "USERPROFILE" in lease_environment:
                lease_environment["HOME"] = lease_environment["USERPROFILE"]
            lease = _HostLease(self, project, node, cli, lease_environment, attempt)
            # A new task cancels any pending idle expiry of the warm child.
            lease.hold = self._holds.lease(self, stop=self._expire, lock=self._lock)
            self._leases.add(lease)
            return lease

    def _retire_uncertain(self):
        """Stop the uncertain session's group, record what is left, and discard it.

        The old scratch root is never reused; it stays on disk when the group
        cannot be confirmed gone, and the note names it for doctor.
        """
        process, root, error = self.process, self.root, self.uncertain
        note = f"Remotion host session retired ({error}); scratch kept at {root}"
        if process is not None:
            try:
                terminate_tree(process, grace_seconds=1.0)
                verify_tree_absent(process)
            except BaseException as exc:
                note += f"; old process group {getattr(process, 'pid', '?')} not confirmed gone: {exc}"
            try:
                self._close_pipes()
            except Exception as exc:
                note += f"; old process pipes not confirmed closed: {exc}"
            self._untrack(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
        if root is not None:
            self._retire(root)
        self.process = self.identity = self.root = self._root_identity = None
        self._stdout_reader = self._stderr_reader = self._stdin_writer = None
        self.uncertain = None
        self._report(note)

    def _capture_stderr(self, chunk):
        # Retain only bounded original-child diagnostics, never env values.
        self._stderr.extend(chunk)
        del self._stderr[:-65536]

    def _close_pipes(self):
        close_pipe_workers(self._stdout_reader, self._stderr_reader, self._stdin_writer)
        self._stdout_reader = self._stderr_reader = self._stdin_writer = None

    def _start(self, lease, identity):
        # Caller renderer identity is only one cache input, never authority to
        # reuse a child launched from different host-selected native inputs.
        identity = (identity, lease.selected_identity)
        if self.process is not None:
            if identity != self.identity:
                self._close_original()
            elif self.process.poll() is None:
                return
            else:
                # The original wait and census, not endpoint absence, settle it.
                self._close_original(already_exited=True)
        if self.root is None:
            self.root = Path(tempfile.mkdtemp(prefix="astrid-remotion-host-", dir=self._root_parent))
            self.root.chmod(0o700)
            root_stat = self.root.lstat()
            self._root_identity = (root_stat.st_dev, root_stat.st_ino, root_stat.st_uid)
        # Fixed host-known renderer projection; never transport Runtime worker
        # credentials, capability grants, arbitrary NODE_OPTIONS or preloaders.
        environment = {key: lease.environment[key] for key in
                       ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH") if key in lease.environment}
        environment.update(ASTRID_FRAME_WORKER_ROOT=str(self.root / "worker"),
                           TMPDIR=str(self.root), TMP=str(self.root), TEMP=str(self.root))
        (self.root / "worker").mkdir(exist_ok=True, mode=0o700)
        try:
            self.process = popen_owned_group(
                [str(lease.node), str(lease.project / "src/astrid-frame-worker.mjs")],
                cwd=str(lease.project), env=build_child_subprocess_env(explicit_env=environment),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except BaseException as error:
            retained = getattr(error, "process", None)
            if retained is not None:
                self.process, self.identity, self.uncertain = retained, identity, error
                self._track(retained)
            raise
        self.identity = identity
        self._track(self.process)
        self._stdout_reader = PipeReader(self.process.stdout)
        self._stderr_reader = PipeReader(self.process.stderr, chunk_size=4096, sink=self._capture_stderr)

    def _exchange(self, request, *, timeout=300.0):
        process = self.process
        observe_tree(process)
        data = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(data) > _LIMIT:
            raise ValueError("renderer request exceeds bound")
        deadline = time.monotonic() + timeout
        self._guard()
        if deadline <= time.monotonic():
            raise TimeoutError("original Remotion worker request write timed out")
        if self._stdin_writer is None:
            self._stdin_writer = PipeWriter(process.stdin)
        try:
            self._stdin_writer.write(data, deadline=deadline, guard=self._guard, poll_interval=0.1)
        except TimeoutError as error:
            raise TimeoutError("original Remotion worker request write timed out") from error
        if self._stdout_reader is None:
            self._stdout_reader = PipeReader(process.stdout)
        response = _read(_PipeReader(self._stdout_reader, timeout=max(0, deadline-time.monotonic()), guard=self._guard))
        observe_tree(process)
        if response.get("cleanup_uncertain") is True:
            self.uncertain = RuntimeError(str(response.get("cleanup_error") or "original Chrome cleanup unresolved"))
        return response

    def _guard(self, *, observe=True):
        if self.root is not None:
            st = self.root.lstat()
            if self.root.is_symlink() or (st.st_dev, st.st_ino, st.st_uid) != self._root_identity:
                raise RuntimeError("original host renderer scratch identity changed")
            self._account(self.root)
        if observe and self.process is not None:
            observe_tree(self.process)

    @staticmethod
    def _inside(path, root, *, exists=True):
        lexical = Path(path)
        canonical = lexical.resolve(strict=exists)
        if not lexical.is_absolute() or canonical != lexical or not canonical.is_relative_to(root):
            raise ValueError("renderer request path escapes admitted task")
        return canonical

    def _validate_request(self, lease, request):
        expected = {"type", "projectDir", "compositionId", "nodeExecutable", "remotionCli",
                    "propsPath", "outputDir", "frames", "resolution", "port", "environment"}
        if set(request) != expected:
            raise ValueError("renderer request has unsupported fields")
        if request.get("projectDir") != str(lease.project) or request.get("nodeExecutable") != str(lease.node):
            raise ValueError("renderer task changed host-selected executable or project")
        if request.get("remotionCli") != str(lease.cli):
            raise ValueError("renderer task changed host-selected Remotion CLI")
        for path, identity in lease.input_identities.items():
            if _file_identity(path) != identity:
                raise ValueError("host-selected renderer input changed after task admission")
        self._inside(request["propsPath"], lease.attempt)
        self._inside(request["outputDir"], lease.attempt, exists=False)
        environment = request.get("environment")
        if not isinstance(environment, dict) or any(not isinstance(v, str) for v in environment.values()):
            raise ValueError("renderer request environment is invalid")
        allowed = {"TMPDIR", "TMP", "TEMP", "ASTRID_TIMELINE_COMPOSITION_SRC", "PYTHONPATH"}
        if set(environment) - allowed:
            raise ValueError("renderer request environment is outside admitted projection")
        for key in ("TMPDIR", "TMP", "TEMP"):
            if key in environment:
                self._inside(environment[key], lease.attempt)
        expected_source = lease.project / "node_modules/@banodoco/timeline-composition/typescript/src"
        if "ASTRID_TIMELINE_COMPOSITION_SRC" in environment and environment["ASTRID_TIMELINE_COMPOSITION_SRC"] != str(expected_source):
            raise ValueError("renderer composition source differs from admitted project")
        if "PYTHONPATH" in environment and environment["PYTHONPATH"] != lease.environment.get("ASTRID_TIMELINE_SCHEMA_PYTHONPATH"):
            raise ValueError("renderer schema environment differs from admission")

    def request(self, lease, frame):
        with self._lock:
            if lease not in self._leases or self._closed:
                raise RuntimeError("renderer task lease is not active")
            if self.uncertain:
                raise RuntimeError("original host renderer cleanup is unresolved") from self.uncertain
            if set(frame) != {"identity", "request"} or not isinstance(frame["identity"], str):
                raise ValueError("invalid renderer lease request")
            request = frame["request"]
            if not isinstance(request, dict) or request.get("type") not in {"render", "release"}:
                raise ValueError("unsupported renderer lease operation")
            if request["type"] == "release":
                if request.get("force"):
                    self._close_original()
                return {"ok": True, "released": True, "exit": bool(request.get("force"))}
            self._validate_request(lease, request)
            try:
                self._start(lease, frame["identity"])
                return self._exchange(request)
            except BaseException as error:
                # A failed transport/start never authorizes replacement.
                if self.process is not None and self.uncertain is None:
                    self.uncertain = error
                raise

    def release(self, lease):
        with self._lock:
            self._leases.discard(lease)
            if lease.hold is not None:
                # After the last lease the host's warm hold arms one idle
                # deadline; an uncertain or absent child is not kept warm.
                warm = self.process is not None and not self.uncertain and not self._closed
                self._holds.release(lease.hold, holding="Remotion frame owner" if warm else None,
                                    on_idle="frame owner shut down", idle_seconds=self._idle_seconds)

    def _expire(self):
        """Idle expiry: the warm hold runs this under self._lock once no lease remains."""
        if self._leases or self._closed or self.uncertain:
            return
        try:
            self._guard()
            self._close_original()
        except BaseException as error:
            self.uncertain = error
            # Custody stays with the child and its scratch; doctor names them.
            self._report(f"Remotion frame owner failed to release after idle ({error}); "
                         f"child {getattr(self.process, 'pid', '?')} and scratch {self.root} retained")

    def _close_original(self, already_exited=False):
        process = self.process
        if process is None:
            if self.root is not None and not self.uncertain:
                self._retire_root()
            return
        try:
            if not already_exited:
                response = self._exchange({"type": "shutdown"}, timeout=5.0)
                if response.get("ok") is not True or response.get("exit") is not True or response.get("shutdown") is not True:
                    raise RuntimeError("original Remotion child did not acknowledge shutdown")
            process.wait(timeout=10.0)
            if process.returncode != 0:
                raise RuntimeError("original Remotion child exited unsuccessfully")
            verify_tree_absent(process)
            if self.uncertain:
                raise RuntimeError("original Remotion cleanup remains sticky") from self.uncertain
            release_group(process)
        except BaseException as error:
            self.uncertain = self.uncertain or error
            raise
        finally:
            if process.poll() is not None:
                try:
                    self._close_pipes()
                except BaseException as error:
                    self.uncertain = self.uncertain or error
                    raise
        try:
            self._retire_root()
        except BaseException as error:
            self.uncertain = self.uncertain or error
            raise
        self._untrack(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        self.process = self.identity = None

    def _retire_root(self):
        self._guard(observe=False)
        root = self.root
        if root is not None:
            shutil.rmtree(root)
            self._retire(root)
        self.root = self._root_identity = None

    def close(self):
        with self._lock:
            self._closed = True
            self._close_original()


class _HostLease:
    def __init__(self, owner, project, node, cli, environment, attempt):
        self.owner, self.project, self.node, self.cli, self.environment = owner, project, node, cli, environment
        self.attempt = attempt
        self.hold = None
        self.input_identities = {p: _file_identity(p) for p in (node, cli, project / "src/astrid-frame-worker.mjs")}
        project_stat = project.stat()
        self.selected_identity = (
            str(project), (project_stat.st_dev, project_stat.st_ino, project_stat.st_uid),
            tuple(sorted((str(path), identity) for path, identity in self.input_identities.items())),
            tuple((key, environment.get(key)) for key in ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH")),
        )
        self.parent, self.child = socket.socketpair()
        self.parent.settimeout(300.0)
        self.child.settimeout(300.0)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def inherited_fd(self):
        return self.child.fileno()

    def launched(self):
        self.child.close()

    def _serve(self):
        try:
            while True:
                frame = _read(self.parent)
                try:
                    response = self.owner.request(self, frame)
                except BaseException as error:
                    response = {"ok": False, "error": str(error), "cleanup_uncertain": bool(self.owner.uncertain)}
                _write(self.parent, response)
        except (EOFError, OSError):
            pass
        finally:
            self.parent.close()
            self.owner.release(self)

    def close(self):
        self.child.close()
        try:
            self.parent.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(timeout=310.0)
        if self.thread.is_alive():
            raise RuntimeError("original renderer lease handler remains active")


class HostRemotionClient:
    def __init__(self, channel):
        self.channel, self.lock = channel, threading.RLock()

    def close(self):
        global _client
        self.channel.close()
        if _client is self:
            _client = None

    def request(self, identity, request, parser):
        with self.lock:
            _write(self.channel, {"identity": identity, "request": request})
            return parser(self.channel)


def install_host_remotion_client(fd):
    global _client
    if _client is not None:
        raise RuntimeError("renderer host lease already installed")
    channel = socket.socket(fileno=int(fd))
    channel.set_inheritable(False)
    channel.settimeout(300.0)
    _client = HostRemotionClient(channel)
    return _client


def host_remotion_client():
    return _client
