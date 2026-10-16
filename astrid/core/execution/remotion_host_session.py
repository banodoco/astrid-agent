"""Private inherited renderer leases; original native custody stays in the host.

This channel is not a Runtime/capability bridge. Only the admitted visualize
executor inherits it, and the host fixes the executable and helper directory.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrid.core.subprocess_env import build_child_subprocess_env

from .process_group import observe_tree, popen_owned_group, release_group, verify_tree_absent
from .remotion_frame_owner import RemotionFrameOwner

_LIMIT = 8 * 1024 * 1024
_client = None
_DARWIN_BROWSER = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


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
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("frame_time_budget_exceeded: original Remotion worker response timed out")
            self.guard()
            if select.select([self.stream], [], [], min(0.1, remaining))[0]:
                return os.read(self.stream.fileno(), count)


@dataclass(frozen=True)
class _FileIdentity:
    security: tuple
    sha256: str
    before: dict = field(compare=False)
    after: dict = field(compare=False)


def _file_facts(st):
    return {name: getattr(st, "st_" + name, 0) for name in
            ("dev", "ino", "mode", "size", "uid", "gid", "flags", "mtime_ns", "ctime_ns")}


def _access_identity(fd):
    # Hash values rather than exporting arbitrary extended-attribute contents.
    attributes = ()
    acl_digest = None
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        library.flistxattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        library.flistxattr.restype = ctypes.c_ssize_t
        library.fgetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                                     ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        library.fgetxattr.restype = ctypes.c_ssize_t
        names = ctypes.create_string_buffer(65536)
        used = library.flistxattr(fd, names, len(names), 0)
        if used < 0 or used >= len(names):
            raise OSError(ctypes.get_errno(), "renderer extended attribute census failed")
        values = []
        for name in names.raw[:used].split(b"\0"):
            if not name:
                continue
            size = library.fgetxattr(fd, name, None, 0, 0, 0)
            if not 0 <= size <= 1024 * 1024:
                raise ValueError("renderer security attribute unavailable or over bound")
            value = ctypes.create_string_buffer(size)
            if library.fgetxattr(fd, name, value, size, 0, 0) != size:
                raise ValueError("renderer security attribute changed during observation")
            values.append((os.fsdecode(name), hashlib.sha256(value.raw).hexdigest()))
        attributes = tuple(sorted(values))
        library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        library.acl_get_fd_np.restype = ctypes.c_void_p
        library.acl_size.argtypes = [ctypes.c_void_p]
        library.acl_size.restype = ctypes.c_ssize_t
        library.acl_copy_ext.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t]
        library.acl_copy_ext.restype = ctypes.c_ssize_t
        library.acl_free.argtypes = [ctypes.c_void_p]
        ctypes.set_errno(0)
        acl = library.acl_get_fd_np(fd, 0x100)  # sys/acl.h ACL_TYPE_EXTENDED
        if not acl:
            # Libc acl_file.c uses filesec_get_property(FILESEC_ACL), whose
            # ENOENT denotes an absent ACL on this already-open file. Other
            # observation failures remain refusals; never discard an ACL error.
            if ctypes.get_errno() != errno.ENOENT:
                raise OSError(ctypes.get_errno(), "renderer input ACL observation failed")
            os.fstat(fd)
            acl_digest = "ABSENT"
        else:
            try:
                size = library.acl_size(acl)
                if not 0 < size <= 1024 * 1024:
                    raise ValueError("renderer ACL unavailable or over bound")
                value = ctypes.create_string_buffer(size)
                if library.acl_copy_ext(value, acl, size) != size:
                    raise ValueError("renderer input ACL serialization failed")
                acl_digest = hashlib.sha256(value.raw).hexdigest()
            finally:
                library.acl_free(acl)
    else:
        attributes = tuple(sorted((name, hashlib.sha256(os.getxattr(fd, name)).hexdigest())
                                  for name in os.listxattr(fd)))
    return attributes, acl_digest


def _file_identity(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("selected renderer input must be canonical and no-follow")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    facts = {}
    try:
        before = _file_facts(os.fstat(descriptor))
        facts["before"] = before
        if not stat.S_ISREG(before["mode"]):
            raise ValueError("selected renderer input is not a regular file")
        access_before = _access_identity(descriptor)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        access_after = _access_identity(descriptor)
        after = _file_facts(os.fstat(descriptor))
        named = _file_facts(path.lstat())
        facts.update(after=after, pathname=named, sha256=digest.hexdigest())
        if (before != after or after != named or access_before != access_after
                or path.resolve(strict=True) != path):
            raise ValueError("selected renderer input changed while hashing or pathname was replaced")
        # ctime/mtime remain stability checks above, and trigger complete secure
        # revalidation on every request; neither is executable replacement alone.
        security = tuple(after[key] for key in ("dev", "ino", "mode", "size", "uid", "gid", "flags"))
        return _FileIdentity((security, access_after), digest.hexdigest(), before, after)
    except BaseException as error:
        error.input_facts = facts
        raise
    finally:
        os.close(descriptor)


def _verify_browser_signature(browser):
    requirement = ('identifier "com.google.Chrome" and anchor apple generic '
                   'and certificate leaf[subject.OU] = "EQHXZ8M8AV"')
    result = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", "-R", requirement, str(browser)],
                            capture_output=True, timeout=10, check=False)
    if result.returncode:
        raise ValueError("host-selected Chrome signature or designated identity failed")


def _host_browser_executable():
    """Select the installed Darwin browser in trusted host code only.

    Never accept an executor browser path or let a missing local browser enter
    Remotion's implicit browser download path. Other platforms retain their
    existing renderer selection.
    """
    if sys.platform != "darwin":
        return None
    browser = _DARWIN_BROWSER
    try:
        if browser.resolve(strict=True) != browser or not browser.is_file() or not os.access(browser, os.X_OK):
            raise ValueError("host-selected Chrome is not a canonical executable")
    except OSError as error:
        raise ValueError("host-selected installed Chrome is unavailable; browser download refused") from error
    _verify_browser_signature(browser)
    return browser


class HostRemotionSession:
    """One host-owned original Node child, shared by sequential task leases."""
    def __init__(self, *, track, untrack, root_parent=None, account=lambda root: None, retire=lambda root: None, closure_binding=None):
        self._lock = threading.RLock()
        self._track, self._untrack = track, untrack
        self._root_parent = root_parent
        from .remotion_closure import RemotionClosureBinding
        if closure_binding is not None and type(closure_binding) is not RemotionClosureBinding:
            raise ValueError("renderer closure must be a trusted host binding")
        self.closure_binding = closure_binding
        self._account, self._retire = account, retire
        self._root_identity = None
        self._custody_root = None
        self.root = None
        self.process = None
        self.identity = None
        self.custody_unresolved = None
        self.execution_invalidated = None
        self._input_facts = []
        self._cleanup_settled = False
        self._closed = False
        self._leases = set()
        self._timer = None
        self._stderr = bytearray()
        self._stderr_thread = None
        self._idle_seconds = 45.0
        self.frame_owner = RemotionFrameOwner.for_host_session(self)

    @property
    def uncertain(self):
        # Compatibility for existing host callers: input invalidation is never
        # a lost cleanup owner. Only custody uncertainty fences retirement.
        return self.custody_unresolved

    @uncertain.setter
    def uncertain(self, error):
        self.custody_unresolved = error

    def _record_inputs(self, lease, phase, observed=None):
        facts = {"phase": phase, "inputs": [
            {"path": str(path), "admitted": {"before": identity.before, "after": identity.after,
             "sha256": identity.sha256, "security_sha256": hashlib.sha256(repr(identity.security).encode()).hexdigest()},
             **({"observed": {"before": observed[path].before, "after": observed[path].after,
                "sha256": observed[path].sha256,
                "security_sha256": hashlib.sha256(repr(observed[path].security).encode()).hexdigest()}}
                if observed is not None and path in observed else {})}
            for path, identity in lease.input_identities.items()]}
        self._input_facts.append(facts)
        del self._input_facts[:-8]
        self._persist_input_state()

    def _persist_input_state(self):
        if self.root is None:
            return
        from .custody_broker import _atomic_owner_json
        self._guard(observe=False)
        def reason(error):
            if error is None:
                return None
            return {"error_type": type(error).__name__,
                    "reason_sha256": hashlib.sha256(str(error).encode()).hexdigest()}
        _atomic_owner_json(self.root / "input-validation.json", {
            "observations": self._input_facts,
            "execution_invalidated": self.execution_invalidated is not None,
            "custody_unresolved": self.custody_unresolved is not None,
            "execution_failure": reason(self.execution_invalidated),
            "custody_failure": reason(self.custody_unresolved),
            "failed_snapshot": getattr(self.execution_invalidated, "input_facts", None),
            "cleanup_settled": self._cleanup_settled,
        })

    def _validate_inputs(self, lease):
        observed = {}
        try:
            if self.closure_binding is not None:
                self.closure_binding.validate(lease.project)
            for path, identity in lease.input_identities.items():
                observed[path] = _file_identity(path)
                if observed[path] != identity:
                    raise ValueError("host-selected renderer input changed after task admission")
            if lease.browser is not None:
                if _host_browser_executable() != lease.browser:
                    raise ValueError("host-selected browser identity changed")
        except BaseException as error:
            self.execution_invalidated = self.execution_invalidated or error
            self._record_inputs(lease, "refused", observed)
            raise
        self._record_inputs(lease, "revalidated", observed)

    def lease(self, environment, *, attempt, scratch):
        """Retain outer task custody and its exact policy-selected artifact root."""
        with self._lock:
            if self._closed or self.uncertain or self.execution_invalidated:
                raise RuntimeError("host Remotion owner is closed or cleanup is unresolved")
            project = Path(environment.get("ASTRID_REMOTION_PROJECT_DIR", ""))
            node = Path(environment.get("ASTRID_NODE_EXECUTABLE", ""))
            if not project.is_absolute() or project.resolve() != project or not node.is_absolute():
                raise ValueError("host renderer lease requires qualified explicit project and Node")
            node = node.resolve(strict=True)
            if not (project / "src/astrid-frame-worker.mjs").is_file() or not node.is_file():
                raise ValueError("host renderer lease selected inputs are missing")
            cli = project / "node_modules/@remotion/cli/remotion-cli.js"
            canonical_cli = cli.resolve(strict=True)
            if not canonical_cli.is_relative_to(project):
                if self.closure_binding is None or self.closure_binding.validate(project) != canonical_cli:
                    raise ValueError("host selected Remotion CLI escapes project without an admitted closure")
            elif not canonical_cli.is_file():
                raise ValueError("host selected Remotion CLI is missing")
            attempt = Path(attempt).resolve(strict=True)
            if not attempt.is_dir():
                raise ValueError("renderer task root is unavailable")
            # Scratch is an explicit host policy input, never an attempt fallback.
            scratch = self._inside(scratch, attempt)
            if scratch == attempt or not scratch.is_dir():
                raise ValueError("renderer scratch must be a private task directory")
            if self._timer:
                self._timer.cancel()
                self._timer = None
            budget = int(environment.get("ASTRID_REMOTION_WARM_MEMORY_BUDGET_BYTES", "0"))
            limit = 300.0 if budget >= 512 * 1024 ** 2 else 45.0
            requested = float(environment.get("ASTRID_TIMELINE_FRAME_IDLE_SECONDS", str(limit)))
            self._idle_seconds = min(limit, max(0.0, requested)) if math.isfinite(requested) else limit
            lease = _HostLease(self, project, node, cli, dict(environment), attempt, scratch)
            self._leases.add(lease)
            return lease

    def _capture_stderr(self, process):
        while True:
            chunk = process.stderr.read(4096)
            if not chunk:
                return
            # Retain only bounded original-child diagnostics, never env values.
            self._stderr.extend(chunk)
            del self._stderr[:-65536]

    def _start(self, lease, identity):
        # Caller renderer identity is only one cache input, never authority to
        # reuse a child launched from different host-selected native inputs.
        identity = (identity, lease.selected_identity)
        if self.process is not None:
            if identity != self.identity:
                self._close_original()
            elif self.process.poll() is None:
                self._guard(observe=False)
                return
            else:
                # The original wait and census, not endpoint absence, settle it.
                self._close_original(already_exited=True)
        if self.root is None:
            # Canonicalize only the host's default system-temp parent (Darwin
            # commonly exposes /var through an alias). Explicit roots remain
            # strict inputs to the binding and are never repaired on reuse.
            parent = self._root_parent if self._root_parent is not None else Path(tempfile.gettempdir()).resolve(strict=True)
            self.root = Path(tempfile.mkdtemp(prefix="astrid-remotion-host-", dir=parent))
            self.root.chmod(0o700)
            root_stat = self.root.lstat()
            self._root_identity = (root_stat.st_dev, root_stat.st_ino, root_stat.st_uid)
        # Storage is host-selected and bound to this warm owner incarnation.
        from .custody_broker import CustodyRootBinding
        if self._custody_root is None:
            self._custody_root = CustodyRootBinding(
                self.root, lifecycle_identity=self._root_identity,
                owner_epoch=os.urandom(32).hex(),
            )
        self._custody_root.verify()
        # Fixed host-known renderer projection; never transport Runtime worker
        # credentials, capability grants, arbitrary NODE_OPTIONS or preloaders.
        environment = {key: lease.environment[key] for key in
                       ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH") if key in lease.environment}
        environment.update(ASTRID_FRAME_WORKER_ROOT=str(self.root / "worker"),
                           TMPDIR=str(self.root), TMP=str(self.root), TEMP=str(self.root))
        (self.root / "worker").mkdir(exist_ok=True, mode=0o700)
        # Revalidate next to launch after lifecycle setup; the opened snapshot
        # does not claim immutable execution or replace confinement.
        self._validate_inputs(lease)
        self._cleanup_settled = False
        try:
            self.process = popen_owned_group(
                [str(lease.node), str(lease.project / "src/astrid-frame-worker.mjs")],
                custody_root=self._custody_root,
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
        self._record_inputs(lease, "launch")
        self._stderr_thread = threading.Thread(target=self._capture_stderr, args=(self.process,), daemon=True)
        self._stderr_thread.start()

    def _exchange(self, request, *, timeout=300.0):
        process = self.process
        observe_tree(process)
        data = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(data) > _LIMIT:
            raise ValueError("renderer request exceeds bound")
        descriptor = process.stdin.fileno()
        blocking = os.get_blocking(descriptor)
        deadline = time.monotonic() + timeout
        try:
            os.set_blocking(descriptor, False)
            pending = memoryview(data)
            while pending:
                self._guard()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("frame_time_budget_exceeded: original Remotion worker request write timed out")
                if not select.select([], [descriptor], [], min(0.1, remaining))[1]:
                    continue
                try:
                    written = os.write(descriptor, pending)
                except BlockingIOError:
                    continue
                pending = pending[written:]
        finally:
            os.set_blocking(descriptor, blocking)
        response = _read(_PipeReader(process.stdout, timeout=max(0, deadline-time.monotonic()), guard=self._guard))
        observe_tree(process)
        if response.get("cleanup_uncertain") is True:
            self.uncertain = RuntimeError(str(response.get("cleanup_error") or "original Chrome cleanup unresolved"))
        return response

    def _guard(self, *, observe=True):
        try:
            self._guard_custody(observe=observe)
        except BaseException as error:
            self.custody_unresolved = self.custody_unresolved or error
            raise

    def _guard_custody(self, *, observe=True):
        if self.root is not None:
            st = self.root.lstat()
            if self.root.is_symlink() or (st.st_dev, st.st_ino, st.st_uid) != self._root_identity:
                raise RuntimeError("original host renderer scratch identity changed")
            if self._custody_root is not None:
                self._custody_root.verify()
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
        if set(request) not in (expected, expected | {"diagnostics"}):
            raise ValueError("renderer request has unsupported fields")
        if "diagnostics" in request and type(request["diagnostics"]) is not bool:
            raise ValueError("renderer diagnostics must be boolean")
        frames = request.get("frames")
        if (not isinstance(frames, list) or not 0 < len(frames) <= 120
                or any(type(frame) is not int or not 0 <= frame <= 2**53 - 1 for frame in frames)):
            raise ValueError("frame_budget_exceeded: require 1..120 non-negative integer frames")
        resolution = request.get("resolution")
        if resolution is not None and (not isinstance(resolution, list) or len(resolution) != 2
                or any(type(size) is not int or not 0 < size <= 4096 for size in resolution)):
            raise ValueError("frame_resolution_budget_exceeded")
        if (not isinstance(request.get("compositionId"), str) or not request["compositionId"]
                or type(request.get("port")) is not int or not 0 < request["port"] < 65536):
            raise ValueError("renderer composition or port is invalid")
        if request["type"] in {"evaluate", "validate"} and request["compositionId"] != "AstridTrackEvaluation":
            raise ValueError("renderer evaluation composition is not admitted")
        if request.get("projectDir") != str(lease.project) or request.get("nodeExecutable") != str(lease.node):
            raise ValueError("renderer task changed host-selected executable or project")
        if request.get("remotionCli") != str(lease.cli):
            raise ValueError("renderer task changed host-selected Remotion CLI")
        self._validate_inputs(lease)
        scratch = self._inside(lease.scratch, lease.attempt)
        observed = scratch.stat()
        if (observed.st_dev, observed.st_ino, observed.st_uid) != lease.scratch_identity:
            raise ValueError("renderer private scratch changed after task admission")
        props = self._inside(request["propsPath"], lease.scratch)
        if not props.is_file() or props.stat().st_size > _LIMIT:
            raise ValueError("renderer props exceed bound or are not a regular file")
        self._inside(request["outputDir"], lease.scratch, exists=False)
        environment = request.get("environment")
        if not isinstance(environment, dict) or any(not isinstance(v, str) for v in environment.values()):
            raise ValueError("renderer request environment is invalid")
        allowed = {"TMPDIR", "TMP", "TEMP", "ASTRID_TIMELINE_COMPOSITION_SRC", "PYTHONPATH"}
        if set(environment) - allowed:
            raise ValueError("renderer request environment is outside admitted projection")
        for key in ("TMPDIR", "TMP", "TEMP"):
            if key in environment:
                self._inside(environment[key], lease.scratch)
        expected_source = lease.project / "node_modules/@banodoco/timeline-composition/typescript/src"
        if "ASTRID_TIMELINE_COMPOSITION_SRC" in environment and environment["ASTRID_TIMELINE_COMPOSITION_SRC"] != str(expected_source):
            raise ValueError("renderer composition source differs from admitted project")
        if "PYTHONPATH" in environment and environment["PYTHONPATH"] != lease.environment.get("ASTRID_TIMELINE_SCHEMA_PYTHONPATH"):
            raise ValueError("renderer schema environment differs from admission")

    def request(self, lease, frame):
        return self.frame_owner.request(lease, frame)

    def _request_owned(self, lease, frame):
        with self._lock:
            if lease not in self._leases or self._closed:
                raise RuntimeError("renderer task lease is not active")
            if self.uncertain:
                raise RuntimeError("original host renderer cleanup is unresolved") from self.uncertain
            if set(frame) != {"identity", "request"} or not isinstance(frame["identity"], str):
                raise ValueError("invalid renderer lease request")
            request = frame["request"]
            if not isinstance(request, dict) or request.get("type") not in {"render", "evaluate", "validate", "release"}:
                raise ValueError("unsupported renderer lease operation")
            if request["type"] == "release":
                if set(request) != {"type", "force"} or type(request["force"]) is not bool:
                    raise ValueError("invalid renderer release")
                if request.get("force"):
                    self._close_original()
                return {"ok": True, "released": True, "exit": bool(request.get("force"))}
            if self.execution_invalidated:
                raise RuntimeError("host renderer execution invalidated") from self.execution_invalidated
            self._validate_request(lease, request)
            admitted_request = request
            try:
                if request["type"] == "validate":
                    # Cache reuse still checks the original owner/scratch and
                    # full lease admission, without allocating a native child.
                    self._guard()
                    return {"ok": True, "validated": True}
                # The executor contract deliberately excludes browserExecutable.
                # Only the host adds the selected, identity-checked native input.
                if lease.browser is not None:
                    request = {**request, "browserExecutable": str(lease.browser)}
                self._start(lease, frame["identity"])
                response = self._exchange(request)
                self._validate_inputs(lease)
                if request["type"] == "evaluate":
                    self._validate_request(lease, admitted_request)
                return response
            except BaseException as error:
                # A failed operation invalidates result/cache acceptance.
                # Custody guards/census retain their own independent fence.
                self.execution_invalidated = self.execution_invalidated or error
                custody_error = getattr(getattr(self.process, "_astrid_custody", None), "_uncertain", None)
                tree_error = getattr(self.process, "_astrid_tree_uncertain", None)
                if custody_error is not None or tree_error is not None:
                    self.custody_unresolved = self.custody_unresolved or custody_error or RuntimeError(tree_error)
                raise

    def release(self, lease):
        with self._lock:
            self._leases.discard(lease)
            if not self._leases and self.process is not None and not self.uncertain and not self._closed:
                # Same bounded retention as the existing qualified owner; the
                # caller supplies only the host-projected allocation fields.
                self._idle_deadline = time.monotonic() + self._idle_seconds
                self._arm_idle()

    def _arm_idle(self):
        self._timer = threading.Timer(min(0.5, max(0, self._idle_deadline-time.monotonic())), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self):
        with self._lock:
            if self._leases or self._closed:
                return
            try:
                self._guard()
                if time.monotonic() < self._idle_deadline:
                    self._arm_idle()
                else:
                    self._close_original()
            except BaseException as error:
                self.uncertain = error

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
            self._cleanup_settled = True
        except BaseException as error:
            self.uncertain = self.uncertain or error
            try:
                self._persist_input_state()
            except (OSError, RuntimeError, ValueError) as persistence_error:
                self._input_state_persistence_error = type(persistence_error).__name__
            raise
        try:
            if self._stderr_thread:
                self._stderr_thread.join(timeout=1.0)
                if self._stderr_thread.is_alive():
                    raise RuntimeError("original Remotion stderr pipe remains live")
            self._retire_root()
        except BaseException as error:
            self.uncertain = self.uncertain or error
            raise
        self._untrack(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        self.process = self.identity = None

    def _retire_root(self):
        if self.uncertain or getattr(getattr(self.process, "_astrid_custody", None), "_uncertain", None):
            raise RuntimeError("unresolved custody evidence must survive retirement")
        self._guard(observe=False)
        root = self.root
        if root is not None and self.execution_invalidated is not None:
            # Preserve failed-input diagnostics under the already accounted
            # host root even after successful cleanup. Retention is evidence,
            # not a live process obligation or custody uncertainty.
            self._persist_input_state()
            self._account(root)
            return
        if root is not None:
            shutil.rmtree(root)
            self._retire(root)
        self.root = self._root_identity = self._custody_root = None

    def close(self):
        with self._lock:
            self._closed = True
            if self._timer:
                self._timer.cancel()
            self._close_original()


class _HostLease:
    def __init__(self, owner, project, node, cli, environment, attempt, scratch):
        self.owner, self.project, self.node, self.cli, self.environment = owner, project, node, cli, environment
        self.attempt, self.scratch = attempt, scratch
        observed = scratch.stat()
        self.scratch_identity = (observed.st_dev, observed.st_ino, observed.st_uid)
        self.browser = _host_browser_executable()
        self.input_identities = {p: _file_identity(p) for p in (node, cli.resolve(strict=True), project / "src/astrid-frame-worker.mjs")}
        if self.browser is not None:
            self.input_identities[self.browser] = _file_identity(self.browser)
        project_stat = project.stat()
        self.selected_identity = (
            str(project), (project_stat.st_dev, project_stat.st_ino, project_stat.st_uid),
            tuple(sorted((str(path), (identity.security, identity.sha256)) for path, identity in self.input_identities.items())),
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
    raise ValueError("renderer socket inheritance is not admitted; use the bound standard-pipe worker")


def host_remotion_client():
    return _client
