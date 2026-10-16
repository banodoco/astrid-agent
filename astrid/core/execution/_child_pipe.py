"""One HostChildBridge consumer on host-created anonymous application pipes.

These pipes carry the unchanged SDK frames after bootstrap. They convey no
credential, caller environment or executable authority. The parent retains the
original Popen/custody; no descriptor number can select another host channel.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading

from .custody_broker import _PipeChannel
from .process_group import process_birth_identity

VERSION = "astrid.child.pipe/v1"
LIMIT = 16384
_consumer_started = False
FIELDS = {"task_id", "run_id", "project_id", "attempt_id", "fence", "runtime_epoch"}


class _ChildPipeChannel(_PipeChannel):
    """Serialize close against bounded I/O so an FD number cannot be reused
    while another bridge thread is about to read or write that number.
    """

    def __init__(self, read_fd, write_fd):
        self._io_lock = threading.RLock()
        self._creator_pid = os.getpid()
        super().__init__(read_fd, write_fd)

    def recv(self, size, flags=0):
        with self._io_lock:
            if os.getpid() != self._creator_pid:
                raise ValueError("child pipe cannot be used by a forked consumer")
            return super().recv(size, flags)

    def sendall(self, data):
        with self._io_lock:
            if os.getpid() != self._creator_pid:
                raise ValueError("child pipe cannot be used by a forked consumer")
            return super().sendall(data)

    def close(self):
        with self._io_lock:
            super().close()


def encode(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(raw) > LIMIT:
        raise ValueError("child pipe bootstrap exceeds bound")
    return raw


def read_frame(channel):
    raw = bytearray()
    while not raw.endswith(b"\n"):
        part = channel.recv(min(4096, LIMIT + 1 - len(raw)))
        if not part:
            raise EOFError("child pipe closed during bootstrap")
        raw.extend(part)
        if len(raw) > LIMIT or b"\n" in raw[:-1]:
            raise ValueError("invalid child pipe bootstrap frame")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate child pipe bootstrap key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    if type(value) is not dict:
        raise ValueError("invalid child pipe bootstrap")
    return value


def pipe_identity(fd, direction):
    import fcntl

    info = os.fstat(fd)
    if (
        not stat.S_ISFIFO(info.st_mode)
        or info.st_nlink != 0
        or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != direction
    ):
        raise ValueError("child bridge requires anonymous directional pipes")
    return {"device": info.st_dev, "inode": info.st_ino, "direction": direction}


def pipe_pair():
    """Create both directions privately; close every partial allocation."""
    fds = []
    try:
        request_read, request_write = os.pipe()
        fds.extend((request_read, request_write))
        reply_read, reply_write = os.pipe()
        fds.extend((reply_read, reply_write))
        return _ChildPipeChannel(request_read, reply_write), _ChildPipeChannel(
            reply_read, request_write
        )
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def validate_binding(binding, channel):
    required = {
        "protocol",
        "context",
        "executor_id",
        "pid",
        "birth",
        "parent_pid",
        "launch_digest",
        "pipes",
    }
    if (
        type(binding) is not dict
        or set(binding) != required
        or binding["protocol"] != VERSION
        or type(binding["pid"]) is not int
        or binding["pid"] != os.getpid()
        or type(binding["parent_pid"]) is not int
        or binding["parent_pid"] != os.getppid()
        or not binding["birth"]
        or binding["birth"] != process_birth_identity()
        or type(binding["launch_digest"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", binding["launch_digest"]) is None
        or type(binding["executor_id"]) is not str
        or not binding["executor_id"]
    ):
        raise ValueError("child pipe differs from selected launch incarnation")
    context = binding["context"]
    if (
        type(context) is not dict
        or set(context) != FIELDS
        or any(type(context[k]) is not int or context[k] < 1 for k in ("fence", "runtime_epoch"))
        or any(
            type(context[k]) is not str or not context[k]
            for k in FIELDS - {"fence", "runtime_epoch"}
        )
    ):
        raise ValueError("child pipe claim binding is invalid")
    if binding["pipes"] != [
        pipe_identity(channel.read_fd, os.O_RDONLY),
        pipe_identity(channel.write_fd, os.O_WRONLY),
    ]:
        raise ValueError("child pipe endpoints differ from selected launch")


class _ChildBridgeLaunchBinding:
    """Single-use host state, never decoded from caller data or environment."""

    def __init__(self, bridge):
        from ._child_bridge import HostChildBridge

        if type(bridge) is not HostChildBridge or bridge._launch_binding is not None:
            raise ValueError("child pipe binding requires its original bridge owner")
        self.bridge = bridge
        self.channel = bridge.child_channel
        self.owner_pid = os.getpid()
        self.owner_birth = process_birth_identity()
        self.context = dict(bridge.context)
        authority = getattr(bridge.host, "_attempt_claim_authority", None)
        if authority is None:
            raise ValueError("child pipe requires the authenticated host claim")
        self.claim = dict(authority.fields)
        self.endpoints = [
            dict(fd=fd, **pipe_identity(fd, direction))
            for fd, direction in (
                (self.channel.read_fd, os.O_RDONLY),
                (self.channel.write_fd, os.O_WRONLY),
            )
        ]
        self.prepared = False
        self.custody = None
        self.wire = None
        self.actor = None
        self.selection = None

    def select(self, argv, cwd, environment, descriptors):
        # Called only by the original host's confinement-plan producer. Keep
        # primitive immutable selection here: the ordinary -m CLI defines its
        # plan class in __main__, rather than in the imported host module.
        self.verify()
        if (
            self.selection is not None
            or type(argv) is not tuple
            or not argv
            or type(cwd) is not str
            or type(environment) is not tuple
            or descriptors != self.descriptors
        ):
            raise ValueError("child pipe requires one exact confined launch plan")
        self.selection = (argv, cwd, environment)

    def verify_launch(self, argv, options, *, new_session):
        import subprocess

        self.verify()
        if (
            self.selection is None
            or self.prepared
            or not new_session
            or tuple(argv) != self.selection[0]
            or str(options.get("cwd")) != self.selection[1]
            or options.get("env") != dict(self.selection[2])
            or options.get("close_fds") is not True
            or any(options.get(name) != subprocess.PIPE for name in ("stdin", "stdout", "stderr"))
        ):
            raise ValueError(
                "child pipe launch differs from admitted executable, policy or environment"
            )

    def assert_running(self):
        process = self.custody.process if self.custody is not None else None
        if process is None or process.poll() is not None:
            raise ValueError("child pipe original command exited")
        if self.actor is None:
            self.actor = self.custody._launch_broker.retained_pipe_actor()
        identity = self.actor.verify()
        if identity["pid"] != process.pid or identity["birth_id"] != process._astrid_process_birth:
            raise ValueError("child pipe command incarnation changed")

    @property
    def descriptors(self):
        self.verify()
        return tuple(row["fd"] for row in self.endpoints)

    def verify(self):
        bridge = self.bridge
        if (
            bridge._launch_binding is not self
            or bridge.child_channel is not self.channel
            or self.owner_pid != os.getpid()
            or self.owner_birth != process_birth_identity()
            or bridge._channel_closed
            or bridge._revoked.is_set()
            or bridge.context != self.context
        ):
            raise ValueError("child pipe owner or attempt changed")
        bridge._live()
        authority = getattr(bridge.host, "_attempt_claim_authority", None)
        current = dict(authority.fields) if authority is not None else None
        if (
            current != self.claim
            or current is not None
            and (
                any(current.get(k) != v for k, v in self.context.items() if k != "run_id")
                or current.get("executor_id") != bridge.host.executor_id
            )
        ):
            raise ValueError("child pipe differs from authenticated claim")
        actual = [
            dict(fd=fd, **pipe_identity(fd, direction))
            for fd, direction in (
                (self.channel.read_fd, os.O_RDONLY),
                (self.channel.write_fd, os.O_WRONLY),
            )
        ]
        if actual != self.endpoints:
            raise ValueError("child pipe descriptor was closed or replaced")

    def prepare(self, custody, argv):
        self.verify()
        if (
            self.prepared
            or self.selection is None
            or tuple(argv) != self.selection[0]
            or custody._launch_broker.role != "command"
        ):
            raise ValueError("child pipe launch binding is reused or has wrong role")
        self.prepared = True
        self.custody = custody
        from .custody_broker import _canonical, _digest_bytes

        self.wire = dict(
            purpose="child_bridge",
            role="command",
            endpoints=self.endpoints,
            argv_digest=_digest_bytes(_canonical(list(argv))),
            owner_epoch=custody.owner_epoch,
            run_id=custody._launch_broker.run_id,
            claim_digest=hashlib.sha256(encode([self.context, self.claim])).hexdigest(),
        )
        return self.wire


def install_bridge(encoded_descriptors):
    """Consume the custody-validated pair before any command or registry code."""
    from astrid.sdk._child_bridge import _install_child_bridge

    from .custody_broker import _CHILD_BRIDGE_CONSUMER, _validate_child_bridge_binding

    global _consumer_started
    if _consumer_started:
        raise ValueError("child pipe consumer cannot be reinstalled")
    _consumer_started = True
    selected = os.environ.pop(_CHILD_BRIDGE_CONSUMER, None)
    if selected is None:
        raise ValueError("child pipe lacks custody bootstrap")
    wire = json.loads(selected)
    descriptors = _validate_child_bridge_binding(wire)
    channel = _ChildPipeChannel(*descriptors)
    try:
        located = json.loads(encoded_descriptors)
        if (
            type(located) is not list
            or any(type(fd) is not int for fd in located)
            or located != list(descriptors)
        ):
            raise ValueError("child pipe argument differs from admitted endpoints")
        channel.settimeout(5.0)
        bootstrap = read_frame(channel)
        if set(bootstrap) != {"binding", "digest"}:
            raise ValueError("invalid child pipe bootstrap envelope")
        binding = bootstrap["binding"]
        digest = hashlib.sha256(encode(binding)).hexdigest()
        if (
            bootstrap["digest"] != digest
            or binding.get("launch_digest") != hashlib.sha256(encode(wire)).hexdigest()
        ):
            raise ValueError("child pipe bootstrap differs from custody launch")
        validate_binding(binding, channel)
        channel.sendall(encode({"ready": digest}))
        if read_frame(channel) != {"accepted": digest}:
            raise ValueError("child pipe launch acceptance differs")
        return _install_child_bridge(channel)
    except BaseException:
        channel.close()
        raise
