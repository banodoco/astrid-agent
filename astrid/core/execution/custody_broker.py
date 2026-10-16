"""Astrid custody consumer for Runtime's protected per-role designation ABI.

The designation/reference formats and cleanup.lock contract are owned by Runtime.
This module consumes that ABI without importing Runtime or granting authority from
an escrow reference. Numeric PIDs select observations; signals use kernel tokens.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import errno
import shutil
import ctypes
import fcntl
import hashlib
import json
import math
import os
import re
import select
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence

# Normal consumers use the selected package mapping. The isolated stdlib
# trampoline loads only its adjacent matched ABI before package startup.
if __package__:
    from . import hosted_contract
else:
    sys.dont_write_bytecode = True
    import importlib.util as _hosted_import
    _hosted_spec = _hosted_import.spec_from_file_location(
        "_matched_hosted_contract", Path(__file__).with_name("hosted_contract.py"))
    hosted_contract = _hosted_import.module_from_spec(_hosted_spec)
    _hosted_spec.loader.exec_module(hosted_contract)


SOL_LOCAL = 0
LOCAL_PEERTOKEN = 0x006
TOKEN_BYTES = 32
TASK_AUDIT_TOKEN = 15
TASK_AUDIT_TOKEN_COUNT = 8
DARWIN_POSIX_SPAWN_SETEXEC = 0x0040
DARWIN_POSIX_SPAWN_START_SUSPENDED = 0x0080
FRAME_LIMIT = 16 * 1024
AUTHORITY_SCOPE_LOCK = "admission.lock"
DESIGNATION_VERSION = "runtime.role-custody-designation/v1"
ROLE_REFERENCE_VERSION = "runtime.role-custody-reference/v1"


class CustodyError(RuntimeError):
    """The exact registered Runtime incarnation cannot be proved or signalled."""


_BOOTSTRAP_PIPES = 'ASTRID_RUNTIME_CUSTODY_PIPES'
_BOOTSTRAP_CHILD_BRIDGE = 'ASTRID_RUNTIME_CUSTODY_CHILD_BRIDGE'
_CHILD_BRIDGE_CONSUMER = 'ASTRID_CHILD_BRIDGE_CHANNEL'


def _pipe_identity(fd: int, direction: int) -> dict[str, int]:
    if type(fd) is not int or fd < 3:
        raise CustodyError('private pipe descriptor is invalid')
    value = os.fstat(fd)
    if (not stat.S_ISFIFO(value.st_mode) or value.st_nlink != 0
            or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != direction):
        raise CustodyError('private channel is not an anonymous directional pipe')
    return {'fd': fd, 'device': value.st_dev, 'inode': value.st_ino, 'direction': direction}


def _validate_pipe_binding(value: object) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise CustodyError('private pipe binding is invalid')
    descriptors = []
    for item, direction in zip(value, (os.O_RDONLY, os.O_WRONLY)):
        if (not isinstance(item, dict) or set(item) != {'fd', 'device', 'inode', 'direction'}
                or any(type(part) is not int for part in item.values())
                or _pipe_identity(item.get('fd'), direction) != item):
            raise CustodyError('private pipe identity differs')
        descriptors.append(item['fd'])
    if descriptors[0] == descriptors[1]:
        raise CustodyError('private pipe endpoints collide')
    return tuple(descriptors)


def _validate_child_bridge_binding(value, *, role='command', argv=None, run_id=None):
    if (type(value) is not dict or set(value) != {
            'purpose', 'role', 'endpoints', 'argv_digest', 'owner_epoch', 'run_id', 'claim_digest'}
            or value['purpose'] != 'child_bridge' or value['role'] != role or role != 'command'
            or type(value['run_id']) is not str or not 0 < len(value['run_id']) <= 128
            or run_id is not None and value['run_id'] != run_id
            or type(value['argv_digest']) is not str
            or re.fullmatch(r'sha256:[0-9a-f]{64}', value['argv_digest']) is None
            or argv is not None and value['argv_digest'] != _digest_bytes(_canonical(argv))
            or type(value['claim_digest']) is not str
            or re.fullmatch(r'[0-9a-f]{64}', value['claim_digest']) is None
            or type(value['owner_epoch']) is not str
            or re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', value['owner_epoch']) is None):
        raise CustodyError('child bridge bootstrap differs from matched command launch')
    return _validate_pipe_binding(value['endpoints'])


class _PipeChannel:
    """Bounded duplex framing over two host-created anonymous pipes.

    Pipe custody is established by authenticated bootstrap, not by message PID
    claims. Endpoints stop inheriting when the matched workload consumes them.
    """

    def __init__(self, read_fd: int, write_fd: int):
        _pipe_identity(read_fd, os.O_RDONLY)
        _pipe_identity(write_fd, os.O_WRONLY)
        self.read_fd, self.write_fd = read_fd, write_fd
        self.timeout = None
        self.buffer = bytearray()
        self.set_inheritable(False)

    @classmethod
    def from_binding(cls, encoded: str):
        return cls(*_validate_pipe_binding(json.loads(encoded)))

    def set_inheritable(self, value):
        for fd in (self.read_fd, self.write_fd):
            os.set_inheritable(fd, value)

    def settimeout(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def recv(self, size, flags=0):
        if flags & ~(socket.MSG_PEEK | socket.MSG_DONTWAIT):
            raise CustodyError('unsupported pipe receive flags')
        if not self.buffer:
            timeout = 0 if flags & socket.MSG_DONTWAIT else self.timeout
            if not select.select([self.read_fd], [], [], timeout)[0]:
                if flags & socket.MSG_DONTWAIT:
                    raise BlockingIOError(errno.EAGAIN, 'pipe has no queued data')
                raise TimeoutError('private pipe read timed out')
            self.buffer.extend(os.read(self.read_fd, size))
        result = bytes(self.buffer[:size])
        if not flags & socket.MSG_PEEK:
            del self.buffer[:size]
        return result

    def sendall(self, data):
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while data:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if not select.select([], [self.write_fd], [], remaining)[1]:
                raise TimeoutError('private pipe write timed out')
            # PIPE_BUF-sized writes cannot block after write readiness.
            amount = os.write(self.write_fd, data[:512])
            data = data[amount:]

    def shutdown(self, _how):
        self.close()

    def close(self):
        for name in ('read_fd', 'write_fd'):
            fd = getattr(self, name)
            if fd >= 0:
                setattr(self, name, -1)
                os.close(fd)


def _close_workload_descriptors(approved: Sequence[int]) -> None:
    # The bootstrap is single-threaded. Enumerate its entire descriptor table;
    # RLIMIT_NOFILE can be unbounded and is not a valid census limit.
    if sys.platform == 'darwin':
        class FDInfo(ctypes.Structure):
            _fields_ = [('fd', ctypes.c_int32), ('kind', ctypes.c_uint32)]
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        library.proc_pidinfo.restype = ctypes.c_int
        size = library.proc_pidinfo(os.getpid(), 1, 0, None, 0)
        if size <= 0:
            raise CustodyError('bootstrap descriptor census unavailable')
        entries = (FDInfo * (size // ctypes.sizeof(FDInfo) + 128))()
        used = library.proc_pidinfo(os.getpid(), 1, 0, ctypes.byref(entries), ctypes.sizeof(entries))
        if used <= 0 or used >= ctypes.sizeof(entries) or used % ctypes.sizeof(FDInfo):
            raise CustodyError('bootstrap descriptor census incomplete')
        descriptors = [entry.fd for entry in entries[:used // ctypes.sizeof(FDInfo)]]
    else:
        raise CustodyError('bootstrap descriptor census is unsupported')
    for fd in descriptors:
        if fd > 2 and fd not in approved:
            os.close(fd)


class _StartAnchorTargetGone(CustodyError):
    """Optional prospective enrollment declined before commit; no cleanup proof."""


class UnadmittedCandidateAbsent(CustodyError):
    """A successful initial lookup observed absence, not exit or subtree closure."""


class NativeTokenCaptureError(CustodyError):
    """Structured acquisition failure; contains no kernel capability material."""

    def __init__(self, message: str, *, substage: str, kern_return: int,
                 port_acquired: bool, count: int | None = None):
        self.capture_failure = {"substage": substage, "kern_return": kern_return,
                                "port_acquired": port_acquired, "count": count}
        super().__init__(message)


def _candidate_observation_evidence(observation: Mapping[str, Any] | None) -> dict | None:
    if observation is None:
        return None
    evidence = {"outcome": observation.get("outcome")}
    for key, fields in (("identity", ("pid", "uid", "parent_pid", "birth_id")),
                        ("native", ("pid", "uid", "parent_pid", "pgid", "status", "start_sec", "start_usec"))):
        if isinstance(observation.get(key), Mapping):
            evidence[key] = {field: observation[key].get(field) for field in fields}
    if observation.get("outcome") == "OBSERVATION_ERROR":
        evidence["error_type"] = type(observation.get("error")).__name__
    return evidence


class _PostTokenCandidateTerminal(CustodyError):
    """Successful capture followed by terminal observation; no designation."""

    def __init__(self, pre, post, token):
        self.candidate_observations = {
            "pre": _candidate_observation_evidence(pre),
            "post": _candidate_observation_evidence(post),
        }
        self.token_identity = {key: token.get(key) for key in ("pid", "uid", "pidversion", "sha256")}
        super().__init__("unadmitted candidate became terminal after token capture")


class UnadmittedCandidateZombie(CustodyError):
    """A birth-bound terminal observation, never custody or retained reap."""

    def __init__(self, observation: Mapping[str, Any]) -> None:
        self.observation = observation
        super().__init__("ordinary candidate observed a birth-bound zombie; exit/reap remains unproved")


def ordinary_candidate_observation(pid: int) -> dict[str, Any]:
    """Observe before token capture; existing authority identity APIs stay strict."""
    try:
        try:
            precise = _darwin_process_start(pid)
        except DarwinProcessExitedError:
            precise = None
        rows = _darwin_process_records(pid)
        if not rows:
            return {"outcome": "ABSENT"}
        if precise is not None and any(precise[key] != rows[0][key]
                                      for key in ("pid", "uid", "start_sec", "start_usec")):
            raise CustodyError("ordinary candidate changed across precise kernel observations")
        row = rows[0]
        identity = {"pid": row["pid"], "uid": row["uid"], "parent_pid": row["parent_pid"],
                    "birth_id": _ps_lstart_birth(row["start_sec"])}
        if row["status"] not in {1, 2, 3, 4, 5}:
            raise CustodyError("ordinary candidate native state is invalid")
        return {"outcome": "ZOMBIE" if row["status"] == 5 else "LIVE",
                "identity": identity, "native": dict(row)}
    except Exception as error:
        return {"outcome": "OBSERVATION_ERROR", "error": error}


class _DarwinExitObservation:
    """One registered incarnation's exit event; never a control capability.

    No child discovery or historical containment inference. The queue remains
    retained across optional Mach capture failure until an event or explicit
    observer close. Absence before registration is recorded as a missed edge.
    """
    def __init__(self, observation):
        self.queue = None
        self._bound = False
        self.facts = {"authority": "NONE", "outcome": "UNREGISTERED",
                      "pre": _candidate_observation_evidence(observation)}
        native = observation.get("native", {})
        pid = native.get("pid")
        try:
            self.queue = select.kqueue()
            before = _darwin_process_start(pid)
            keys = ("pid", "uid", "start_sec", "start_usec")
            if any(before.get(key) != native.get(key) for key in keys):
                raise CustodyError("exit observation incarnation changed before registration")
            self.facts["registration_pre"] = before
            self.queue.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT)], 0, 0)
            self.facts["outcome"] = "REGISTERED"
            # Retain the actual event even across a terminal post-check, but
            # bind it to this incarnation only after a matching live bracket.
            # PID reuse between pre-check and registration otherwise remains
            # indistinguishable from the original process exiting here.
            self.poll()
            try:
                after = _darwin_process_start(pid)
            except DarwinProcessExitedError:
                self.poll()
                self.facts["registration_post"] = {"outcome": "TERMINAL_OR_ABSENT"}
                if "event" not in self.facts:
                    self.facts["outcome"] = "TERMINAL_DURING_REGISTRATION_UNOBSERVED"
                self.close()
            else:
                self.facts["registration_post"] = after
                if any(before[key] != after[key] for key in keys + ("start_abstime",)):
                    self.facts["outcome"] = "INCARNATION_CHANGED"
                    self.close()
                else:
                    self._bound = True
                    self.facts["registration_bound"] = True
                    if self.facts["outcome"] == "EXIT_EVENT_UNBOUND":
                        self.facts["outcome"] = "EXIT_OBSERVED"
        except (OSError, AttributeError, CustodyError, TypeError) as error:
            self.facts.update(outcome="REGISTRATION_UNAVAILABLE", error_type=type(error).__name__,
                              errno=getattr(error, "errno", None))
            self.close()

    def poll(self):
        if self.queue is None:
            return False
        try:
            events = self.queue.control([], 1, 0)
            if not events:
                return False
            event = events[0]
            native = self.facts["pre"]["native"]
            if (event.ident != native["pid"] or event.filter != select.KQ_FILTER_PROC
                    or event.flags & select.KQ_EV_ERROR or not event.fflags & select.KQ_NOTE_EXIT):
                self.facts["outcome"] = "EVENT_UNVERIFIED"
            else:
                self.facts.update(outcome="EXIT_OBSERVED" if self._bound else "EXIT_EVENT_UNBOUND", event={
                    "pid": event.ident, "filter": event.filter, "fflags": event.fflags,
                    "flags": event.flags, "data": event.data})
            self.close()
            return True
        except OSError as error:
            self.facts.update(outcome="EVENT_UNAVAILABLE", errno=error.errno)
            self.close()
            return True

    def close(self):
        if self.queue is not None:
            self.queue.close()
            self.queue = None


class AuditTokenSignalError(CustodyError):
    """A kernel signal rejection with a definitely unsuccessful return code."""

    def __init__(self, result: int, observed_errno: int) -> None:
        self.result, self.observed_errno = result, observed_errno
        super().__init__(
            "proc_signal_with_audittoken failed for registered target "
            f"(returncode={result}, errno={observed_errno})"
        )


class DarwinProcessExitedError(CustodyError):
    """A process-start query found absence/zombie; not descendant exit proof."""

class _SignalCaptureTargetGone(DarwinProcessExitedError):
    """Target exited during its initial precommit anchored capture."""


class _SignalTargetGone(DarwinProcessExitedError):
    """Initial target-only pre-signal observation; never cleanup completion."""


# Public Darwin sysctl structures, matching the 64-bit SDK kinfo_proc ABI.
# This census is an observation, never a wait record or signal capability.
class _DarwinTimeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_int64), ("tv_usec", ctypes.c_int32)]


class _DarwinItimerval(ctypes.Structure):
    _fields_ = [("interval", _DarwinTimeval), ("value", _DarwinTimeval)]


class _DarwinProcUnion(ctypes.Union):
    _fields_ = [("start", _DarwinTimeval), ("links", ctypes.c_void_p * 2)]


class _DarwinExternProc(ctypes.Structure):
    _fields_ = [
        ("un", _DarwinProcUnion), ("vmspace", ctypes.c_void_p), ("sigacts", ctypes.c_void_p),
        ("flag", ctypes.c_int32), ("status", ctypes.c_int8), ("pid", ctypes.c_int32),
        ("oppid", ctypes.c_int32), ("dupfd", ctypes.c_int32), ("user_stack", ctypes.c_void_p),
        ("exit_thread", ctypes.c_void_p), ("debugger", ctypes.c_int32), ("sigwait", ctypes.c_int32),
        ("estcpu", ctypes.c_uint32), ("cpticks", ctypes.c_int32), ("pctcpu", ctypes.c_uint32),
        ("wchan", ctypes.c_void_p), ("wmesg", ctypes.c_void_p), ("swtime", ctypes.c_uint32),
        ("slptime", ctypes.c_uint32), ("realtimer", _DarwinItimerval), ("rtime", _DarwinTimeval),
        ("uticks", ctypes.c_uint64), ("sticks", ctypes.c_uint64), ("iticks", ctypes.c_uint64),
        ("traceflag", ctypes.c_int32), ("tracep", ctypes.c_void_p), ("siglist", ctypes.c_int32),
        ("textvp", ctypes.c_void_p), ("holdcnt", ctypes.c_int32), ("sigmask", ctypes.c_uint32),
        ("sigignore", ctypes.c_uint32), ("sigcatch", ctypes.c_uint32), ("priority", ctypes.c_uint8),
        ("usrpri", ctypes.c_uint8), ("nice", ctypes.c_int8), ("comm", ctypes.c_char * 17),
        ("pgrp", ctypes.c_void_p), ("addr", ctypes.c_void_p), ("xstat", ctypes.c_uint16),
        ("acflag", ctypes.c_uint16), ("ru", ctypes.c_void_p),
    ]


class _DarwinPCred(ctypes.Structure):
    _fields_ = [("lock", ctypes.c_char * 72), ("ucred", ctypes.c_void_p),
                ("ruid", ctypes.c_uint32), ("svuid", ctypes.c_uint32),
                ("rgid", ctypes.c_uint32), ("svgid", ctypes.c_uint32), ("refcnt", ctypes.c_int32)]


class _DarwinUCred(ctypes.Structure):
    _fields_ = [("ref", ctypes.c_int32), ("uid", ctypes.c_uint32),
                ("ngroups", ctypes.c_int16), ("groups", ctypes.c_uint32 * 16)]


class _DarwinVMSpace(ctypes.Structure):
    _fields_ = [("dummy0", ctypes.c_int32), ("dummy2", ctypes.c_void_p),
                ("dummy3", ctypes.c_int32 * 5), ("dummy4", ctypes.c_void_p * 3)]


class _DarwinEProc(ctypes.Structure):
    _fields_ = [
        ("paddr", ctypes.c_void_p), ("sess", ctypes.c_void_p), ("pcred", _DarwinPCred),
        ("ucred", _DarwinUCred), ("vm", _DarwinVMSpace), ("ppid", ctypes.c_int32),
        ("pgid", ctypes.c_int32), ("jobc", ctypes.c_int16), ("tdev", ctypes.c_int32),
        ("tpgid", ctypes.c_int32), ("tsess", ctypes.c_void_p), ("wmesg", ctypes.c_char * 8),
        ("xsize", ctypes.c_int32), ("xrssize", ctypes.c_int16), ("xccount", ctypes.c_int16),
        ("xswrss", ctypes.c_int16), ("flag", ctypes.c_int32), ("login", ctypes.c_char * 12),
        ("spare", ctypes.c_int32 * 4),
    ]


class _DarwinKInfoProc(ctypes.Structure):
    _fields_ = [("proc", _DarwinExternProc), ("eproc", _DarwinEProc)]


def _check_darwin_census_abi() -> None:
    # Reject an unsupported ABI rather than parsing an apparently empty census.
    expected = (
        ctypes.sizeof(ctypes.c_void_p) == 8,
        ctypes.sizeof(ctypes.c_int) == 4,
        sys.byteorder == "little",
        ctypes.sizeof(_DarwinTimeval) == 16,
        ctypes.sizeof(_DarwinExternProc) == 296,
        ctypes.sizeof(_DarwinEProc) == 352,
        ctypes.sizeof(_DarwinKInfoProc) == 648,
        _DarwinExternProc.pid.offset == 40,
        _DarwinExternProc.status.offset == 36,
        _DarwinEProc.ucred.offset + _DarwinUCred.uid.offset == 124,
        _DarwinEProc.ppid.offset == 264,
        _DarwinEProc.pgid.offset == 268,
    )
    if sys.platform != "darwin" or not all(expected):
        raise CustodyError("Darwin public process census ABI is unsupported")


def _ps_lstart_birth(start_sec: int) -> str:
    """Render the English ps-lstart token in UTC, including space-padded day.

    Its second resolution is compatibility data; exact kernel tokens and start
    anchors still provide the existing authority checks. It is not a new PID
    reuse guarantee, and sysctl microseconds are not silently discarded there.
    UTC and fixed English names keep identity independent of observer TZ/locale.
    """
    try:
        value = time.gmtime(start_sec)
    except (OverflowError, OSError, ValueError) as exc:
        raise CustodyError("Darwin process census birth is invalid") from exc
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return (f"ps-lstart:{weekdays[value.tm_wday]} {months[value.tm_mon - 1]} "
            f"{value.tm_mday:2d} {value.tm_hour:02d}:{value.tm_min:02d}:{value.tm_sec:02d} {value.tm_year:04d}")


def _darwin_process_records(pid: int | None = None) -> list[dict[str, int]]:
    """Read the public BSD census directly, including protected UIDs and zombies.

    A selected PID's absence is only an observation. Neither absence nor a
    zombie is a positive retained-parent wait/reap or cleanup receipt.
    """
    _check_darwin_census_abi()
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 0x7fffffff):
        raise CustodyError("Darwin process census selector is invalid")
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    except OSError as exc:
        raise CustodyError("Darwin process census library is unavailable") from exc
    library.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    library.sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 4)(1, 14, 0 if pid is None else 1, 0 if pid is None else pid)
    size = ctypes.c_size_t()
    ctypes.set_errno(0)
    if library.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0:
        raise CustodyError(f"Darwin process census sizing failed (errno={ctypes.get_errno()})")
    stride, limit = ctypes.sizeof(_DarwinKInfoProc), 16 * 1024 * 1024
    # Reserve bounded headroom for arrivals between the size and fill calls.
    capacity = int(size.value) + 128 * stride
    if size.value % stride or not 0 < capacity <= limit:
        raise CustodyError("Darwin process census sizing is malformed or over limit")
    buffer = ctypes.create_string_buffer(capacity)
    used = ctypes.c_size_t(capacity)
    ctypes.set_errno(0)
    if library.sysctl(mib, 4, buffer, ctypes.byref(used), None, 0) != 0:
        raise CustodyError(f"Darwin process census read failed (errno={ctypes.get_errno()})")
    if used.value > capacity or used.value % stride:
        raise CustodyError("Darwin process census returned a truncated record")
    if not used.value and pid is None:
        raise CustodyError("Darwin public process census is unavailable")
    rows, seen = [], set()
    for offset in range(0, int(used.value), stride):
        item = _DarwinKInfoProc.from_buffer_copy(buffer, offset)
        p, e = item.proc, item.eproc
        sec, usec = int(p.un.start.tv_sec), int(p.un.start.tv_usec)
        if (p.pid < 0 or p.pid in seen or (pid is not None and p.pid != pid)
                or p.status not in {1, 2, 3, 4, 5, 6} or e.ppid < 0 or e.pgid < 0
                or sec < 0 or (p.pid > 0 and sec == 0) or not 0 <= usec < 1000000):
            raise CustodyError("Darwin public process census contains an invalid identity")
        seen.add(int(p.pid))
        rows.append({"pid": int(p.pid), "uid": int(e.ucred.uid), "parent_pid": int(e.ppid),
                     "pgid": int(e.pgid), "status": int(p.status), "start_sec": sec,
                     "start_usec": usec})
    return rows


def _darwin_command_metadata(pid: int) -> dict[str, Any]:
    """Public, bounded observation only. Never read/persist the environment."""
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    bsd = _ProcBSDInfo()
    if library.proc_pidinfo(pid, 3, 0, ctypes.byref(bsd), ctypes.sizeof(bsd)) != ctypes.sizeof(bsd) or bsd.pid != pid:
        raise CustodyError("diagnostic BSD metadata unavailable")
    path = ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
    if library.proc_pidpath(pid, path, len(path)) <= 0:
        raise CustodyError("diagnostic executable path unavailable")
    kernel = ctypes.CDLL(None, use_errno=True)
    kernel.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                             ctypes.c_void_p, ctypes.c_size_t]
    kernel.sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN / KERN_PROCARGS2
    data = ctypes.create_string_buffer(64 * 1024)
    used = ctypes.c_size_t(len(data))
    if kernel.sysctl(mib, 3, data, ctypes.byref(used), None, 0) != 0:
        raise CustodyError("diagnostic argument observation unavailable or over limit")
    try:
        arguments = _diagnostic_argv(bytes(data[:used.value]))
    except CustodyError:
        arguments = {"argv_outcome": "UNAVAILABLE_OR_AMBIGUOUS", "environment": "NOT_EMITTED"}
    return {"executable": os.fsdecode(path.value),
            "bsd_identity": {"pid": int(bsd.pid), "parent_pid": int(bsd.ppid),
                             "uid": int(bsd.uid), "real_uid": int(bsd.ruid),
                             "saved_uid": int(bsd.svuid), "status": int(bsd.status),
                             "start_sec": int(bsd.start_sec), "start_usec": int(bsd.start_usec)},
            **arguments}


def _diagnostic_argv(data: bytes) -> dict[str, Any]:
    """Decode only argc arguments from KERN_PROCARGS2, never following env."""
    if not 5 <= len(data) <= 64 * 1024:
        raise CustodyError("diagnostic argument buffer is malformed")
    argc = int.from_bytes(data[:4], sys.byteorder, signed=True)
    if not 1 <= argc <= 256:
        raise CustodyError("diagnostic argument count is malformed or over limit")
    end = data.find(b"\0", 4)
    if end < 0:
        raise CustodyError("diagnostic executable argument prefix is malformed")
    executable_prefix = os.fsdecode(data[4:end])
    offset = end + 1
    while offset < len(data) and data[offset] == 0:
        offset += 1
    argv = []
    for _ in range(argc):
        end = data.find(b"\0", offset)
        if end < 0:
            raise CustodyError("diagnostic argument vector is truncated")
        argv.append(os.fsdecode(data[offset:end])); offset = end + 1
    # NUL padding cannot distinguish an empty argv[0]. Require its public
    # executable spelling, and reject any apparent environment boundary
    # instead of allowing a malformed argc to consume environment entries.
    if (not argv[0] or argv[0] != executable_prefix
            or any("=" in value for value in argv)):
        raise CustodyError("diagnostic argv/environment boundary is ambiguous")
    safe, redact_next, opaque_next = [], False, False
    secret = re.compile(r"(?:^|[-_])(token|password|secret|credential|api[-_]?key)(?:$|[-_=])", re.I)
    for index, value in enumerate(argv):
        if opaque_next:
            safe.append("<opaque:" + _digest_bytes(os.fsencode(value)) + ">")
            opaque_next = False
            continue
        if redact_next:
            safe.append("<redacted>"); redact_next = False; continue
        if value.startswith("-") and secret.search(value):
            flag, separator, _ = value.partition("=")
            safe.append(flag + "=<redacted>" if separator else flag)
            redact_next = not separator
        elif (value in {"-c", "-e", "-p", "--eval", "--print"}
              or (value.startswith("-") and not value.startswith("--")
                  and "c" in value[1:] and Path(argv[0]).name in {"sh", "bash", "zsh", "dash", "ksh"})):
            safe.append(value); opaque_next = True
        elif (index == 0 or value == "" or re.fullmatch(r"--?[A-Za-z0-9_-]+", value)
              or (len(value) <= 2048 and value.startswith("/")
                  and re.fullmatch(r"[A-Za-z0-9_./-]+", value))
              or re.fullmatch(r"--(?:concurrency|frames|scale|port|fps)=[0-9.,]+", value)):
            safe.append(value)
        else:
            # Shell/JS/JSON/assignment and unknown option values are opaque.
            # Their digest permits comparison to a known source invocation.
            safe.append("<opaque:" + _digest_bytes(os.fsencode(value)) + ">")
    return {"argc": argc, "argv": safe, "argv_sha256": _digest_bytes(_canonical(argv)),
            "environment": "EXCLUDED", "argv_redacted_or_truncated": safe != argv}


def _command_observation(pid: int, *, native_reader=None, command_reader=None) -> dict[str, Any]:
    """Bracket metadata by public identity/path. Exec transition stays unproved."""
    native_reader = native_reader or _darwin_process_records
    command_reader = command_reader or _darwin_command_metadata
    try:
        before = native_reader(pid)
        if len(before) != 1 or before[0]["status"] not in {1, 2, 3, 4}:
            return {"outcome": "NOT_LIVE", "authority": "NONE"}
        first = command_reader(pid)
        after = native_reader(pid)
        second = command_reader(pid)
        fields = ("pid", "uid", "parent_pid", "start_sec", "start_usec")
        matched = (len(after) == 1 and after[0]["status"] in {1, 2, 3, 4}
                   and all(before[0].get(k) == after[0].get(k) for k in fields)
                   and first == second)
        return {"outcome": "BRACKETED_METADATA" if matched else "CHANGED_DURING_OBSERVATION",
                "native_before": before[0], "native_after": after[0] if after else None,
                "metadata_first": first, "metadata_second": second,
                "exec_transition": "UNPROVED_PUBLIC_METADATA_IS_NOT_ATOMIC_EXEC_IDENTITY",
                "authority": "NONE"}
    except Exception as error:
        return {"outcome": "OBSERVATION_ERROR", "error_type": type(error).__name__,
                "authority": "NONE"}


def _capture_failure_command_evidence(metadata, expected_rows) -> dict[str, Any]:
    """Correlate optional redacted metadata; never confer process authority."""
    fields = ("pid", "uid", "parent_pid", "pgid", "start_sec", "start_usec")
    try:
        matched = (isinstance(metadata, Mapping)
                   and metadata.get("outcome") == "BRACKETED_METADATA"
                   and bool(expected_rows))
        for expected in expected_rows:
            matched = matched and isinstance(expected, Mapping)
            for native in (metadata.get("native_before"), metadata.get("native_after")):
                matched = (matched and isinstance(native, Mapping)
                           and all(type(expected.get(key)) is int and native.get(key) == expected[key]
                                   for key in fields)
                           and expected.get("pid", 0) > 0
                           and expected.get("uid", -1) >= 0
                           and native.get("status") in {1, 2, 3, 4})
            if isinstance(expected, Mapping) and expected.get("birth_id") is not None:
                matched = (matched and expected["birth_id"] ==
                           _ps_lstart_birth(expected["start_sec"]))
        return {"classification": "MATCHED_PRE_POST_CENSUS_METADATA" if matched else "UNPROVED",
                "authority": "NONE", "exec_identity": "UNPROVED",
                "observation": metadata}
    except BaseException as error:
        return {"classification": "UNPROVED", "authority": "NONE", "exec_identity": "UNPROVED",
                "error_type": type(error).__name__}


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved",
    )] + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
        (name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")
    ] + [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


class _RUsageInfoV0(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        "user", "system", "idle", "interrupt", "pageins", "wired", "resident",
        "footprint", "start_abstime", "exit_abstime",
    )]


def _darwin_process_start(pid: int) -> dict[str, int]:
    """Read public exec-stable birth data; a zombie never grants authority."""
    if sys.platform != "darwin" or isinstance(pid, bool) or pid <= 0:
        raise CustodyError("Darwin process-start custody is unavailable")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    library.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    library.proc_pid_rusage.restype = ctypes.c_int
    bsd, usage = _ProcBSDInfo(), _RUsageInfoV0()
    ctypes.set_errno(0)
    if library.proc_pidinfo(pid, 3, 0, ctypes.byref(bsd), ctypes.sizeof(bsd)) != ctypes.sizeof(bsd):
        if ctypes.get_errno() == errno.ESRCH:
            raise DarwinProcessExitedError("Darwin process-start query found no live process")
        raise CustodyError("Darwin process-start BSD observation is unavailable")
    if library.proc_pid_rusage(pid, 0, ctypes.byref(usage)) != 0:
        raise CustodyError("Darwin monotonic process-start observation is unavailable")
    if bsd.status == 5 or usage.exit_abstime:
        raise DarwinProcessExitedError("Darwin process-start query found an exited process")
    if bsd.pid != pid or bsd.status not in {1, 2, 3, 4} or not usage.start_abstime:
        raise CustodyError("Darwin process-start identity is absent, exited or invalid")
    return {"pid": int(bsd.pid), "uid": int(bsd.uid),
            "start_abstime": int(usage.start_abstime),
            "start_sec": int(bsd.start_sec), "start_usec": int(bsd.start_usec),
            "status": int(bsd.status)}


def _darwin_absolute_time() -> int:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    library.mach_absolute_time.argtypes = []
    library.mach_absolute_time.restype = ctypes.c_uint64
    return int(library.mach_absolute_time())


def _start_anchor(observed: Mapping[str, object]) -> dict[str, int]:
    keys = ("pid", "uid", "start_abstime", "start_sec", "start_usec")
    if any(isinstance(observed.get(key), bool) or not isinstance(observed.get(key), int)
           or int(observed[key]) < (1 if key in {"pid", "start_abstime"} else 0) for key in keys):
        raise CustodyError("Darwin process-start anchor is invalid")
    if observed.get("status") not in {1, 2, 3, 4}:
        raise CustodyError("Darwin process-start anchor is not live")
    return {key: int(observed[key]) for key in keys}


def _validated_token(token: Mapping[str, object], anchor: Mapping[str, object]) -> list[int]:
    words = token.get("words")
    if (not isinstance(words, list) or token.get("pid") != anchor["pid"]
            or token.get("uid") != anchor["uid"]
            or audit_token_details(words) != {key: token.get(key) for key in ("pid", "uid", "pidversion", "sha256")}):
        raise CustodyError("Darwin current exact token differs from the established start anchor")
    return list(words)


class _AuditToken(ctypes.Structure):
    _fields_ = [("value", ctypes.c_uint32 * 8)]


def _admitted_signals() -> set[int]:
    admitted = {int(signal.SIGTERM), int(signal.SIGKILL)}
    if hasattr(signal, "SIGUSR1"):
        admitted.add(int(signal.SIGUSR1))
    return admitted


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _send_frame(connection: socket.socket, value: Mapping[str, object]) -> None:
    encoded = _canonical(dict(value)) + b"\n"
    if len(encoded) > FRAME_LIMIT:
        raise CustodyError("custody frame exceeds its hard limit")
    connection.sendall(encoded)


def _read_frame(connection: socket.socket) -> dict[str, object]:
    raw = bytearray()
    while not raw.endswith(b"\n"):
        part = connection.recv(min(4096, FRAME_LIMIT + 1 - len(raw)))
        if not part:
            raise CustodyError("custody peer closed before a complete frame")
        raw.extend(part)
        if len(raw) > FRAME_LIMIT:
            raise CustodyError("custody frame exceeds its hard limit")
    if b"\n" in raw[:-1]:
        raise CustodyError("custody frame contains trailing data")
    value = json.loads(bytes(raw[:-1]).decode("utf-8"))
    if not isinstance(value, dict):
        raise CustodyError("custody frame is not an object")
    return value


def _token_from_words(words: Sequence[int]) -> _AuditToken:
    if len(words) != 8 or any(
        isinstance(value, bool) or not isinstance(value, int)
        or value < 0 or value > 0xFFFFFFFF
        for value in words
    ):
        raise CustodyError("registered audit token is invalid")
    token = _AuditToken()
    for index, value in enumerate(words):
        token.value[index] = value
    return token


def audit_token_details(words: Sequence[int]) -> dict[str, object]:
    if sys.platform != "darwin":
        raise CustodyError("Darwin audit-token custody is unavailable")
    token = _token_from_words(words)
    libbsm = ctypes.CDLL("/usr/lib/libbsm.0.dylib", use_errno=True)
    libbsm.audit_token_to_pid.argtypes = [_AuditToken]
    libbsm.audit_token_to_pid.restype = ctypes.c_int
    libbsm.audit_token_to_pidversion.argtypes = [_AuditToken]
    libbsm.audit_token_to_pidversion.restype = ctypes.c_int
    raw = bytes(token)
    return {
        "pid": int(libbsm.audit_token_to_pid(token)),
        "pidversion": int(libbsm.audit_token_to_pidversion(token)),
        "uid": int(token.value[1]),
        "sha256": _digest_bytes(raw),
    }


def _peer_token(connection: socket.socket) -> dict[str, object]:
    if sys.platform != "darwin":
        raise CustodyError("Darwin audit-token custody is unavailable")
    raw = connection.getsockopt(SOL_LOCAL, LOCAL_PEERTOKEN, TOKEN_BYTES)
    if len(raw) != TOKEN_BYTES:
        raise CustodyError("LOCAL_PEERTOKEN returned an invalid token size")
    token = _AuditToken.from_buffer_copy(raw)
    details = audit_token_details([int(value) for value in token.value])
    return {**details, "words": [int(value) for value in token.value]}


def current_process_audit_token(pid: int) -> dict[str, object]:
    """Read one live process token through a retained Mach task-name port."""

    if sys.platform != "darwin" or int(pid) <= 0:
        raise CustodyError("Darwin audit-token custody is unavailable")
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.task_name_for_pid.argtypes = [
        ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32),
    ]
    library.task_name_for_pid.restype = ctypes.c_int
    library.task_info.argtypes = [
        ctypes.c_uint32, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_uint32),
    ]
    library.task_info.restype = ctypes.c_int
    library.pid_for_task.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_int)]
    library.pid_for_task.restype = ctypes.c_int
    library.mach_port_deallocate.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    library.mach_port_deallocate.restype = ctypes.c_int
    task_self = ctypes.c_uint32.in_dll(library, "mach_task_self_").value
    task_name = ctypes.c_uint32(0)
    result = int(library.task_name_for_pid(task_self, int(pid), ctypes.byref(task_name)))
    if result != 0:
        raise NativeTokenCaptureError(f"final-ready Runtime task-name port is unavailable (substage=task_name_for_pid, kern_return={result}, candidate_pid={pid})",
                                      substage="task_name_for_pid", kern_return=result,
                                      port_acquired=bool(task_name.value))

    def read_token(substage: str) -> list[int]:
        token = _AuditToken()
        count = ctypes.c_uint32(TASK_AUDIT_TOKEN_COUNT)
        result = int(library.task_info(
            task_name.value,
            TASK_AUDIT_TOKEN,
            ctypes.cast(ctypes.byref(token), ctypes.POINTER(ctypes.c_int32)),
            ctypes.byref(count),
        ))
        if result != 0 or count.value != TASK_AUDIT_TOKEN_COUNT:
            raise NativeTokenCaptureError(f"final-ready Runtime audit token is unavailable (substage={substage}, kern_return={result}, count={count.value}, candidate_pid={pid})",
                                          substage=substage, kern_return=result,
                                          port_acquired=True, count=int(count.value))
        return [int(value) for value in token.value]

    primary_error = None
    try:
        first = read_token("task_info_first")
        bound_pid = ctypes.c_int(0)
        result = int(library.pid_for_task(task_name.value, ctypes.byref(bound_pid)))
        if result != 0 or bound_pid.value != int(pid):
            raise NativeTokenCaptureError(f"final-ready Runtime task port changed process (substage=pid_for_task, kern_return={result}, candidate_pid={pid}, bound_pid={bound_pid.value})",
                                          substage="pid_for_task", kern_return=result, port_acquired=True)
        second = read_token("task_info_second")
        if first != second:
            raise NativeTokenCaptureError("final-ready Runtime audit token changed while binding",
                                          substage="token_consistency", kern_return=0, port_acquired=True)
        return {**audit_token_details(first), "words": first}
    except BaseException as error:
        primary_error = error
        raise
    finally:
        result = int(library.mach_port_deallocate(task_self, task_name.value))
        if type(primary_error) is NativeTokenCaptureError:
            primary_error.capture_failure["port_release_succeeded"] = result == 0
        if result != 0:
            release_error = NativeTokenCaptureError(f"final-ready Runtime task-name port release failed (substage=mach_port_deallocate, kern_return={result}, candidate_pid={pid})",
                                                   substage="mach_port_deallocate", kern_return=result, port_acquired=True)
            if primary_error is not None:
                primary_error.port_release_error = release_error
                primary_error.add_note(str(release_error))
            else:
                raise release_error


def _authenticated_bootstrap_anchor(pid: int, peer_token: Mapping[str, object]) -> dict[str, int]:
    """Bind exec-stable birth data while the socket-authenticated peer is live.

    Darwin invalidates a pre-exec task-name port at this SETEXEC boundary.
    The existing strict clock fence prevents a later recycled PID from sharing
    this birth tick; token and precise start observations bracket the fence.
    """
    anchor = _start_anchor(_darwin_process_start(pid))
    if (current_process_audit_token(pid) != peer_token
            or _start_anchor(_darwin_process_start(pid)) != anchor
            or _darwin_absolute_time() <= anchor['start_abstime']):
        raise CustodyError('bootstrap start anchor differs from authenticated peer')
    if (_start_anchor(_darwin_process_start(pid)) != anchor
            or current_process_audit_token(pid) != peer_token):
        raise CustodyError('authenticated bootstrap changed across its birth fence')
    return anchor


def _bootstrap_anchored_token(pid: int, anchor: Mapping[str, int]) -> dict[str, object]:
    if _start_anchor(_darwin_process_start(pid)) != anchor:
        raise CustodyError('bootstrap incarnation changed before token capture')
    token = current_process_audit_token(pid)
    if _start_anchor(_darwin_process_start(pid)) != anchor:
        raise CustodyError('bootstrap incarnation changed during token capture')
    return token


def signal_audit_token(words: Sequence[int], signum: int) -> None:
    if int(signum) not in _admitted_signals():
        raise CustodyError("custody signal is outside the admitted set")
    if sys.platform != "darwin":
        raise CustodyError("Darwin audit-token custody is unavailable")
    token = _token_from_words(words)
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_signal_with_audittoken
    function.argtypes = [ctypes.POINTER(_AuditToken), ctypes.c_int]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = int(function(ctypes.byref(token), int(signum)))
    observed_errno = int(ctypes.get_errno())
    if result != 0:
        raise AuditTokenSignalError(result, observed_errno)


def _resume_audit_token_once(words: Sequence[int]) -> None:
    """Resume one suspended launch through its exact, already-bound token."""

    if sys.platform != "darwin":
        raise CustodyError("Darwin audit-token custody is unavailable")
    token = _token_from_words(words)
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_signal_with_audittoken
    function.argtypes = [ctypes.POINTER(_AuditToken), ctypes.c_int]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = int(function(ctypes.byref(token), int(signal.SIGCONT)))
    observed_errno = int(ctypes.get_errno())
    if result != 0:
        raise CustodyError(
            "proc_signal_with_audittoken failed for the one-shot launch resume "
            f"(returncode={result}, errno={observed_errno})"
        )


def _posix_spawn_setexec_suspended(argv: Sequence[str], env: Mapping[str, str]) -> None:
    """Replace this trusted wrapper with argv[0], stopped before user code."""

    if sys.platform != "darwin":
        raise CustodyError("suspended SETEXEC custody is available only on Darwin")
    if not argv or any(not isinstance(value, str) or "\0" in value for value in argv):
        raise CustodyError("suspended SETEXEC argv is invalid")
    if any(not isinstance(key, str) or not isinstance(value, str) or "\0" in key or "\0" in value
           for key, value in env.items()):
        raise CustodyError("suspended SETEXEC environment is invalid")

    library = ctypes.CDLL(None, use_errno=True)
    attr = ctypes.c_void_p()
    library.posix_spawnattr_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    library.posix_spawnattr_init.restype = ctypes.c_int
    library.posix_spawnattr_setflags.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_short]
    library.posix_spawnattr_setflags.restype = ctypes.c_int
    library.posix_spawnattr_destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    library.posix_spawnattr_destroy.restype = ctypes.c_int
    library.posix_spawn.argtypes = [
        ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_char_p),
        ctypes.POINTER(ctypes.c_char_p),
    ]
    library.posix_spawn.restype = ctypes.c_int

    result = int(library.posix_spawnattr_init(ctypes.byref(attr)))
    if result != 0:
        raise OSError(result, os.strerror(result), "posix_spawnattr_init")
    try:
        flags = DARWIN_POSIX_SPAWN_SETEXEC | DARWIN_POSIX_SPAWN_START_SUSPENDED
        result = int(library.posix_spawnattr_setflags(ctypes.byref(attr), ctypes.c_short(flags)))
        if result != 0:
            raise OSError(result, os.strerror(result), "posix_spawnattr_setflags")
        encoded_argv = [os.fsencode(value) for value in argv] + [None]
        encoded_env = [os.fsencode(f"{key}={value}") for key, value in env.items()] + [None]
        argv_array = (ctypes.c_char_p * len(encoded_argv))(*encoded_argv)
        env_array = (ctypes.c_char_p * len(encoded_env))(*encoded_env)
        child = ctypes.c_int(-1)
        result = int(library.posix_spawn(
            ctypes.byref(child), encoded_argv[0], None, ctypes.byref(attr),
            argv_array, env_array,
        ))
        if result != 0:
            raise OSError(result, os.strerror(result), argv[0])
        # POSIX_SPAWN_SETEXEC must replace this process on success. A return
        # means the platform did not honor the required exec-like contract.
        raise CustodyError("suspended SETEXEC unexpectedly returned to the wrapper")
    finally:
        library.posix_spawnattr_destroy(ctypes.byref(attr))


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise CustodyError("custody write was incomplete")
        offset += written


def _atomic_owner_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, _canonical(dict(value)) + b"\n")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _open_authority_scope_lock(scope_root: Path) -> int:
    root = Path(os.path.abspath(scope_root))
    if not root.is_absolute() or root.is_symlink():
        raise CustodyError("custody authority scope is unsafe")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    observed_root = os.lstat(root)
    if (
        not stat.S_ISDIR(observed_root.st_mode)
        or observed_root.st_uid != os.getuid()
        or stat.S_IMODE(observed_root.st_mode) != 0o700
    ):
        raise CustodyError("custody authority scope is not owner-only")
    descriptor = os.open(
        root / AUTHORITY_SCOPE_LOCK,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    os.set_inheritable(descriptor, False)
    observed = os.fstat(descriptor)
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise CustodyError("custody authority scope lock is not owner-only")
    return descriptor


def _read_owner_file(path: Path) -> tuple[bytes, os.stat_result]:
    if path.is_symlink():
        raise CustodyError("custody file must not be a symlink")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise CustodyError("custody file is not an owner-only regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), observed
    finally:
        os.close(descriptor)


def _incarnation(identity: Mapping[str, object], token: Mapping[str, object]) -> dict[str, object]:
    pid, uid, birth = identity.get("pid"), identity.get("uid"), identity.get("birth_id")
    if (
        isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
        or isinstance(uid, bool) or not isinstance(uid, int) or uid < 0
        or not isinstance(birth, str) or not birth
        or token.get("pid") != pid or token.get("uid") != uid
        or isinstance(token.get("pidversion"), bool) or not isinstance(token.get("pidversion"), int)
        or not isinstance(token.get("sha256"), str)
    ):
        raise CustodyError("custody incarnation lacks authenticated identity")
    return {"pid": pid, "uid": uid, "birth_id": birth,
            "audit_token_sha256": token["sha256"], "audit_token_pidversion": token["pidversion"]}


class AuthenticatedCleanupActor:
    """An actor pinned from our own kernel identity or a retained private peer.

    RPC payloads never construct this object. The retained channel (or our own
    process) supplies the token again at each use, including replay.
    """

    def __init__(self, token_reader: Callable[[], Mapping[str, object]],
                 identity_provider: Callable[[int], Mapping[str, object] | None]) -> None:
        self._token_reader = token_reader
        self._identity_provider = identity_provider
        token = dict(token_reader())
        identity = identity_provider(int(token.get("pid", 0)))
        if identity is None:
            raise CustodyError("cleanup actor incarnation is unavailable")
        self._pinned = _incarnation(identity, token)

    @classmethod
    def current(cls, *, identity_provider: Callable[[int], Mapping[str, object] | None] | None = None) -> AuthenticatedCleanupActor:
        return cls(lambda: current_process_audit_token(os.getpid()),
                   identity_provider or default_process_identity)

    @classmethod
    def private_peer(cls, channel: socket.socket,
                     *, identity_provider: Callable[[int], Mapping[str, object] | None] | None = None) -> AuthenticatedCleanupActor:
        return cls(lambda: _peer_token(channel), identity_provider or default_process_identity)

    def verify(self) -> dict[str, object]:
        token = dict(self._token_reader())
        identity = self._identity_provider(int(self._pinned["pid"]))
        if identity is None or _incarnation(identity, token) != self._pinned:
            raise CustodyError("designated cleanup actor incarnation changed or is unknown")
        return dict(self._pinned)


class RetainedChildActor:
    """Positive exit proof only from a handle retained while its peer was live.

    PID absence/reuse, socket EOF and ps failures cannot create exit evidence.
    Nonchild recovery must instead replay an explicit durable relinquishment.
    """

    def __init__(self, child: subprocess.Popen[bytes], actor: AuthenticatedCleanupActor,
                 *, parent: AuthenticatedCleanupActor | None = None) -> None:
        pinned = actor.verify()
        if child.pid != pinned["pid"] or child.poll() is not None:
            raise CustodyError("cleanup actor is not this live retained child")
        self._child = child
        self._pinned = pinned
        self._parent = None
        if parent is not None:
            identity = actor._identity_provider(child.pid)
            parent_identity = parent.verify()
            if (not isinstance(child, RetainedCustodyPopen) or identity is None
                    or parent_identity['pid'] != os.getpid()
                    or identity.get('parent_pid') != parent_identity['pid']):
                raise CustodyError('recovery requires the original retained direct-child parent')
            self._parent = parent_identity

    def prove_parent_wait(self, expected, parent):
        """Stricter recovery evidence; a cached Popen exit is insufficient."""
        if (self._parent is None or self._parent['pid'] != os.getpid()
                or parent.verify() != self._parent
                or dict(expected) != self._pinned
                or not isinstance(self._child, RetainedCustodyPopen)):
            raise CustodyError('reservation recovery has no authenticated retained parent')
        self.prove_exit(expected)
        outcome = self._child._custody_wait_outcome
        if (outcome is None or outcome.get('pid') != self._pinned['pid']
                or not (os.WIFEXITED(outcome['status']) or os.WIFSIGNALED(outcome['status']))
                or os.waitstatus_to_exitcode(outcome['status']) != outcome.get('returncode')
                or outcome['returncode'] != self._child.returncode):
            raise CustodyError('reservation recovery has no positive native parent wait')
        return {'producer': 'retained-parent-native-wait', 'parent': dict(self._parent),
                'old_actor': dict(self._pinned), 'wait': dict(outcome)}

    def prove_exit(self, expected: Mapping[str, object]) -> None:
        if dict(expected) != self._pinned or self._child.poll() is None:
            raise CustodyError("designated cleanup actor has no positive retained-child exit proof")


class RetainedCustodyPopen(subprocess.Popen):
    """Popen retaining a positive native terminal wait status.

    CPython's cached returncode can be zero after ECHILD. Neither that cache
    nor an externally reaped child supplies this owner's positive wait result.
    """

    def __init__(self, *args, **kwargs):
        self._custody_wait_outcome = None
        super().__init__(*args, **kwargs)

    def _try_wait(self, wait_flags):
        # Preserve ECHILD as evidence loss rather than manufacturing status 0.
        return os.waitpid(self.pid, wait_flags)

    def _handle_exitstatus(self, status, **kwargs):
        super()._handle_exitstatus(status, **kwargs)
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            self._custody_wait_outcome = {'pid': self.pid, 'status': status,
                                          'returncode': self.returncode}


class _UnspawnedReservation:
    """Owner-local evidence invalidated before entering the Popen producer."""

    def __init__(self, authority, record, actor):
        self.actor = actor
        self.authority = authority
        self.record = authority._semantic(record)
        self.unattempted = True


class RetainedLaunchChild:
    """Original live direct-child handle, pinned before launch release.

    This object is never reconstructed from a designation or an observed PID.
    Only its own Popen.wait result can produce the terminal evidence.
    """

    def __init__(self, child, *, authority, record, actor, identity_provider):
        identity = identity_provider(child.pid)
        pinned_actor = actor.verify()
        target = record['target']
        if (not isinstance(child, RetainedCustodyPopen) or child.poll() is not None
                or record['actor'] != pinned_actor or identity is None
                or identity.get('parent_pid') != pinned_actor['pid']
                or any(identity.get(k) != target.get(k) for k in ('pid', 'uid', 'birth_id'))
                or child.pid != target.get('pid')):
            raise CustodyError('launch settlement requires the original live direct child')
        self._child = child
        self._binding = {'scope_root': str(authority.root), 'role': authority.role,
                         'generation': record['generation'], 'owner_epoch': record['owner_epoch'],
                         'actor': pinned_actor,
                         'incarnation': {k: target[k] for k in ('pid', 'uid', 'birth_id')}}

    def prove_wait(self, authority, record):
        binding = {'scope_root': str(authority.root), 'role': authority.role,
                   'generation': record['generation'], 'owner_epoch': record['owner_epoch'],
                   'actor': record['actor'],
                   'incarnation': {k: record['target'].get(k) for k in ('pid', 'uid', 'birth_id')}}
        if binding != self._binding:
            raise CustodyError('retained child settlement binding differs')
        try:
            code = self._child.wait(timeout=0)
        except (subprocess.TimeoutExpired, ChildProcessError) as exc:
            raise CustodyError('launch has no positive retained-child wait outcome') from exc
        outcome = self._child._custody_wait_outcome
        if (not isinstance(outcome, dict) or outcome.get('pid') != self._binding['incarnation']['pid']
                or type(code) is not int or outcome.get('returncode') != code):
            raise CustodyError('launch has no positive native retained-child wait outcome')
        return {'producer': 'original-direct-child-wait', 'wait': dict(outcome), **self._binding}


class RoleCustodyAuthority:
    """Protected per-role designation, fenced through every destructive call.

    The atomic designation contains its bounded transition history, so one
    fsynced rename is the commit point and a lost ACK has an exact replay.
    Runtime ownership epochs are metadata; only a role transition advances its
    custody generation. No operation sweeps or invalidates delegated roles.
    """

    def __init__(self, scope_root: Path, role: str, *, timeout: float = 5.0) -> None:
        if not role or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in role):
            raise CustodyError("custody role is invalid")
        if not scope_root.is_absolute() or scope_root.is_symlink():
            raise CustodyError("custody scope is unsafe")
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
            raise CustodyError("custody guard timeout is invalid")
        descriptor = _open_authority_scope_lock(scope_root)
        os.close(descriptor)
        self.root, self.role, self.timeout = scope_root, role, timeout
        self.path = scope_root / ("designation-" + role + ".json")
        # Disk records alone cannot restore fresh-token authority after loss of
        # this live, authenticated custody session (including a reboot).
        self._initial_capture_witness = None
        self._live_start_anchor: dict[str, int] | None = None
        self._anchor_actor: AuthenticatedCleanupActor | None = None
        self._anchor_transition: dict[str, object] | None = None
        # Only this instance's successful writes establish admission provenance.
        # A matching owner file cannot reconstruct the retained cleanup proof.
        self._retained_record: dict[str, object] | None = None
        self._ledger_broken = False
        self._ready_base: dict[str, object] | None = None
        self._admission_prefix: list[dict[str, object]] | None = None
        self._refresh_ledger: list[dict[str, object]] = []
        self._admission_refresh_ledger: list[dict[str, object]] = []
        self._admission_base: dict[str, object] | None = None
        self._ready_committed = False
        self._anchor_requires_resume = False
        self._admission_resume: dict[str, object] | None = None
        self._unspawned_reservation: _UnspawnedReservation | None = None

    @contextmanager
    def _guard(self, *, exclusive: bool):
        descriptor = os.open(self.root / "cleanup.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.getuid() or stat.S_IMODE(observed.st_mode) != 0o600:
                raise CustodyError("custody cleanup guard is unsafe")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CustodyError("custody cleanup guard timed out; authority unresolved")
                    time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read(self) -> dict[str, object]:
        raw, _ = _read_owner_file(self.path)
        if len(raw) > 256 * 1024:
            raise CustodyError("custody designation exceeds its bound")
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise CustodyError("custody designation is malformed") from exc
        if (
            not isinstance(record, dict)
            or set(record) != {"version", "role", "generation", "owner_epoch", "state", "actor", "target", "transitions", "digest"}
            or record["version"] != DESIGNATION_VERSION or record["role"] != self.role
            or isinstance(record["generation"], bool) or not isinstance(record["generation"], int) or record["generation"] < 1
            or record["state"] not in {"pending", "active", "settled", "retired"}
            or not isinstance(record["actor"], dict) or not isinstance(record["target"], dict)
            or not isinstance(record["transitions"], list)
            or record["digest"] != _digest_bytes(_canonical({k: v for k, v in record.items() if k != "digest"}))
        ):
            raise CustodyError("custody designation is invalid")
        return record

    def _write(self, record: dict[str, object]) -> None:
        record = {k: v for k, v in record.items() if k != "digest"}
        record["digest"] = _digest_bytes(_canonical(record))
        if len(_canonical(record)) > 256 * 1024:
            raise CustodyError("custody transition history exceeds its bound")
        _atomic_owner_json(self.path, record)

    @staticmethod
    def _semantic(record: Mapping[str, object]) -> dict[str, object]:
        return json.loads(_canonical({k: v for k, v in record.items() if k != "digest"}))

    def _read_current(self) -> dict[str, object]:
        try:
            record = self._read()
        except BaseException:
            self._ledger_broken = True
            raise
        if self._retained_record is not None and _canonical(self._semantic(record)) != _canonical(self._retained_record):
            self._ledger_broken = True
            raise CustodyError("custody designation differs from retained admission history")
        return record

    def _commit(self, record: dict[str, object], *, initial: bool = False) -> None:
        expected = self._semantic(record)
        try:
            self._write(record)
        except BaseException:
            self._ledger_broken = True
            raise
        if initial or self._retained_record is not None:
            self._retained_record = expected

    def break_token_chain(self) -> None:
        """A failed broker checkpoint cannot later be repaired from disk."""
        self._ledger_broken = True

    def commit_ready(self) -> None:
        with self._guard(exclusive=True):
            self._read_current()
            if self._ledger_broken or self._ready_base is None or self._ready_committed:
                raise CustodyError("custody readiness baseline is unavailable or already committed")
            self._ready_committed = True

    def ready_reference(self) -> dict[str, object]:
        with self._guard(exclusive=False):
            self._read_current()
            if self._ledger_broken or not self._ready_committed or self._ready_base is None:
                raise CustodyError("custody readiness baseline is not durably published")
            return json.loads(_canonical(self._ready_base))

    def token_chain(self, *, actor: AuthenticatedCleanupActor, generation: int,
                    base_target: Mapping[str, object]) -> dict[str, object]:
        """Prove the retained admission prefix and post-ready token suffix.

        This historical check observes the cleanup actor, never the listener.
        A chain proves no death, reap, or arbitrary-descendant completeness.
        """
        with self._guard(exclusive=False):
            if (self._ledger_broken or self._retained_record is None or not self._ready_committed
                    or self._ready_base is None or self._admission_prefix is None
                    or type(generation) is not int):
                raise CustodyError("retained readiness token chain is unavailable")
            try:
                record = self._read_current()
            except OSError as exc:
                raise CustodyError("retained custody designation is unavailable") from exc
            if record["state"] != "active":
                raise CustodyError("retained readiness token chain is inactive")
            self._check_actor(record, actor, generation)
            anchor = self._anchor_transition
            if (anchor is None or self._live_start_anchor is None or self._anchor_actor is None
                    or anchor not in self._admission_prefix
                    or anchor["intent"]["generation"] != generation
                    or anchor["intent"]["requester"] != record["actor"]
                    or anchor["intent"]["start_anchor"] != self._live_start_anchor
                    or self._anchor_actor.verify() != record["actor"]
                    or (self._anchor_requires_resume and self._admission_resume not in self._admission_prefix)
                    or self._ready_base != {**self._reference(record), "target": dict(base_target)}
                    or record["transitions"] != self._admission_prefix + self._refresh_ledger):
                raise CustodyError("custody token chain admission or anchor binding differs")
            previous = dict(base_target)
            transition_ids = []
            for entry in self._refresh_ledger:
                intent, ack = entry["intent"], entry["ack"]
                if (set(entry) != {"transition_id", "intent", "ack"}
                        or not isinstance(entry["transition_id"], str) or not entry["transition_id"]
                        or entry["transition_id"] in transition_ids
                        or set(intent) != {"action", "generation", "requester", "previous_target", "start_anchor"}
                        or intent["action"] != "refresh-exec-token" or type(intent["generation"]) is not int
                        or intent["generation"] != generation or intent["requester"] != record["actor"]
                        or intent["start_anchor"] != self._live_start_anchor or intent["previous_target"] != previous
                        or set(ack) != {"target"}
                        or set(ack["target"]) != set(base_target)
                        or any(ack["target"].get(k) != base_target.get(k) for k in ("pid", "uid", "birth_id"))):
                    raise CustodyError("custody refresh suffix differs from retained token chain")
                transition_ids.append(entry["transition_id"])
                previous = ack["target"]
            current = self._reference(record)
            if current["target"] != previous:
                raise CustodyError("custody current target differs from token chain")
            proof = {"base": dict(base_target), "anchor_transition_id": anchor["transition_id"],
                     "transition_ids": transition_ids, "current": current["target"]}
            return {"revision": len(self._refresh_ledger), "chain": json.loads(_canonical(self._refresh_ledger)),
                    "current_reference": current, "refresh_proof_digest": _digest_bytes(_canonical(proof))}

    def admission_chain(self, *, actor: AuthenticatedCleanupActor, generation: int,
                        base_target: Mapping[str, object]) -> dict[str, object]:
        """Historical authenticated admission suffix, never a ready baseline.

        A pending reference can prove only its exact unbound obligation; neither
        kind of chain proves exit, a reap, or descendant completeness.
        """
        with self._guard(exclusive=False):
            if self._ledger_broken or self._retained_record is None or self._ready_base is not None:
                raise CustodyError("retained admission token chain is unavailable")
            record = self._read_current()
            self._check_actor(record, actor, generation)
            current = self._reference(record)
            if set(record['target']) == {'admission_id'}:
                if record['state'] != 'pending' or dict(base_target) != record['target']:
                    raise CustodyError("pending admission chain has a different obligation")
                return {'revision': 0, 'chain': [], 'current_reference': current,
                        'admission_refresh_digest': _digest_bytes(_canonical(current))}
            anchor = self._anchor_transition
            if (record['state'] != 'active' or self._admission_base is None
                    or anchor is None or self._live_start_anchor is None or self._anchor_actor is None
                    or anchor not in record['transitions'] or self._anchor_actor.verify() != record['actor']
                    or anchor['intent']['generation'] != generation
                    or anchor['intent']['requester'] != record['actor']
                    or anchor['intent']['start_anchor'] != self._live_start_anchor):
                raise CustodyError("admission chain has no retained live anchor")
            entries = self._admission_refresh_ledger
            disk_entries = [e for e in record['transitions']
                            if e.get('intent', {}).get('action') == 'refresh-exec-token'
                            and e.get('intent', {}).get('generation') == generation]
            if disk_entries != entries:
                raise CustodyError("admission refresh ledger differs from retained history")
            previous = dict(self._admission_base)
            targets = [previous]; ids = []
            for entry in entries:
                intent, ack = entry['intent'], entry['ack']
                if (set(entry) != {'transition_id', 'intent', 'ack'}
                        or not isinstance(entry['transition_id'], str) or not entry['transition_id']
                        or entry['transition_id'] in ids
                        or set(intent) != {'action', 'generation', 'requester', 'previous_target', 'start_anchor'}
                        or intent['action'] != 'refresh-exec-token' or type(intent['generation']) is not int
                        or intent['generation'] != generation or intent['requester'] != record['actor']
                        or intent['start_anchor'] != self._live_start_anchor or intent['previous_target'] != previous
                        or set(ack) != {'target'} or set(ack['target']) != set(previous)
                        or any(ack['target'].get(k) != self._admission_base.get(k) for k in ('pid', 'uid', 'birth_id'))):
                    raise CustodyError("admission refresh suffix is invalid")
                ids.append(entry['transition_id']); previous = ack['target']; targets.append(previous)
            if current['target'] != previous or dict(base_target) not in targets:
                raise CustodyError("admission base is not an authenticated chain member")
            offset = targets.index(dict(base_target)); suffix = entries[offset:]
            proof = {'base': dict(base_target), 'current': current['target'], 'anchor': anchor,
                     'role': self.role, 'generation': generation, 'owner_epoch': record['owner_epoch'],
                     'suffix': suffix}
            return {'revision': len(suffix), 'chain': json.loads(_canonical(suffix)),
                    'current_reference': current, 'admission_refresh_digest': _digest_bytes(_canonical(proof))}

    def designate_pending(self, *, actor: AuthenticatedCleanupActor, identity: Mapping[str, object],
                          token: Mapping[str, object], owner_epoch: str) -> None:
        if not isinstance(owner_epoch, str) or not owner_epoch or len(owner_epoch) > 256:
            raise CustodyError("custody owner epoch is invalid")
        with self._guard(exclusive=True):
            if self.path.exists():
                raise CustodyError("custody role already has a designation")
            self._commit({"version": DESIGNATION_VERSION, "role": self.role, "generation": 1,
                         "owner_epoch": owner_epoch, "state": "pending", "actor": actor.verify(),
                         "target": _incarnation(identity, token), "transitions": []}, initial=True)

    def reserve_before_spawn(self, *, actor: AuthenticatedCleanupActor, owner_epoch: str,
                             admission_id: str) -> None:
        """Reserve once after an explicitly terminal prior generation."""
        if (not isinstance(owner_epoch, str) or not owner_epoch or len(owner_epoch) > 256
                or not isinstance(admission_id, str) or not admission_id or len(admission_id) > 256):
            raise CustodyError("pending launch custody binding is invalid")
        with self._guard(exclusive=True):
            generation, transitions = 1, []
            if self.path.exists():
                previous = self._read_current()
                if previous['state'] not in {'settled', 'retired'}:
                    raise CustodyError("custody role already has a designation")
                terminal = previous['transitions'][-1] if previous['transitions'] else {}
                if (terminal.get('intent', {}).get('action') != ('settle-child' if previous['state'] == 'settled' else 'retire-unspawned')
                        or terminal.get('ack', {}).get('generation') != previous['generation']
                        or terminal.get('ack', {}).get('status') != previous['state']):
                    raise CustodyError('custody terminal transition is unavailable')
                generation = previous['generation'] + 1
                transitions = previous['transitions']
            record = {"version": DESIGNATION_VERSION, "role": self.role, "generation": generation,
                      "owner_epoch": owner_epoch, "state": "pending", "actor": actor.verify(),
                      "target": {"admission_id": admission_id}, "transitions": transitions}
            record['transitions'] = [*transitions, self._spawn_transition(record, 'never-exported')]
            self._commit(record, initial=True)
            # The new generation starts without a prior live admission session.
            self._initial_capture_witness = None
            self._live_start_anchor = None
            self._anchor_actor = None
            self._anchor_transition = None
            self._ready_base = None
            self._admission_prefix = None
            self._refresh_ledger = []
            self._admission_refresh_ledger = []
            self._admission_base = None
            self._ready_committed = False
            self._anchor_requires_resume = False
            self._admission_resume = None
            self._unspawned_reservation = _UnspawnedReservation(self, record, actor)

    @staticmethod
    def _spawn_transition(record, status):
        return {'transition_id': 'spawn-' + str(record['generation']) + '-' + status,
                'intent': {'action': 'spawn-authority', 'role': record['role'],
                           'generation': record['generation'], 'owner_epoch': record['owner_epoch'],
                           'requester': record['actor'], 'target': record['target']},
                'ack': {'status': status}}

    def _never_exported(self, record):
        # Old/missing/uncertain records cannot acquire this new recovery right.
        # Exactly one generation-bound entry must be the entire current suffix.
        entries = [entry for entry in record['transitions']
                   if entry.get('intent', {}).get('generation') == record['generation']]
        expected = self._spawn_transition(record, 'never-exported')
        return (record['state'] == 'pending' and set(record['target']) == {'admission_id'}
                and _canonical(entries) == _canonical([expected])
                and _canonical(record['transitions'][-1:]) == _canonical([expected]))

    def begin_spawn(self) -> None:
        """Durably fence recovery BEFORE any export or creation producer.

        No exception (including Popen failure) restores never-exported state.
        This lock also serializes a late creation attempt against retirement.
        """
        with self._guard(exclusive=True):
            proof = self._unspawned_reservation
            if proof is None or self._ledger_broken:
                raise CustodyError('spawn attempt has no retained reservation')
            record = self._read_current()
            self._check_actor(record, proof.actor, proof.record['generation'])
            if (record['state'] not in {'pending', 'active'}
                    or record['owner_epoch'] != proof.record['owner_epoch']):
                raise CustodyError('spawn attempt has a terminal or changed reservation')
            exported = self._spawn_transition(proof.record, 'may-have-exported')
            if not proof.unattempted:
                if exported not in record['transitions']:
                    raise CustodyError('spawn authority durable fence is unavailable')
                return
            if not self._never_exported(record) or self._semantic(record) != proof.record:
                raise CustodyError('spawn authority has uncertain reservation provenance')
            proof.unattempted = False  # Failed/uncertain writes cannot restore local proof.
            record['transitions'].append(exported)
            self._commit(record)  # file + directory fsync before capability escapes.

    def retain_child(self, *, actor: AuthenticatedCleanupActor, generation: int,
                     child: subprocess.Popen, identity_provider) -> RetainedLaunchChild:
        with self._guard(exclusive=False):
            record = self._read_current()
            self._check_actor(record, actor, generation)
            if record['state'] != 'active':
                raise CustodyError('retained child requires active authenticated registration')
            return RetainedLaunchChild(child, authority=self, record=record, actor=actor,
                                       identity_provider=identity_provider)

    def settle(self, *, actor: AuthenticatedCleanupActor, generation: int,
               owner_epoch: str, expected_target: Mapping[str, object], transition_id: str,
               retained_child: RetainedLaunchChild | None = None,
               unspawned: _UnspawnedReservation | None = None,
               dead_actor: RetainedChildActor | None = None) -> dict[str, object]:
        """Durably settle one exact child wait or retire its unattempted owner.

        An exact committed transition may be replayed without recreating lost
        wait evidence. An uncommitted wait lost with its owner stays unresolved.
        A surviving pinned parent may retire only a durable never-exported
        reservation with its original positive native wait for the old actor.
        """
        if (type(generation) is not int or generation < 1
                or not isinstance(transition_id, str) or not transition_id or len(transition_id) > 256
                or not isinstance(owner_epoch, str) or not owner_epoch
                or not isinstance(expected_target, Mapping)):
            raise CustodyError('custody settlement binding is invalid')
        action = 'retire-unspawned' if set(expected_target) == {'admission_id'} else 'settle-child'
        requester = actor.verify()
        intent = {'action': action, 'role': self.role, 'generation': generation,
                  'owner_epoch': owner_epoch, 'requester': requester, 'target': dict(expected_target)}
        with self._guard(exclusive=True):
            record = self._read()
            for entry in record['transitions']:
                if entry.get('transition_id') == transition_id:
                    if entry.get('intent') != intent:
                        raise CustodyError('custody settlement replay conflicts with durable intent')
                    return dict(entry['ack'])
            record = self._read_current()
            if dead_actor is None:
                self._check_actor(record, actor, generation)
            elif (action != 'retire-unspawned' or record['generation'] != generation
                  or unspawned is not None):
                raise CustodyError('reservation recovery has a stale generation or wrong target')
            # Authenticate again under the same authority lock as proof/commit.
            if actor.verify() != requester:
                raise CustodyError('custody settlement actor changed')
            if (record['owner_epoch'] != owner_epoch or self._reference(record)['target'] != dict(expected_target)
                    or record['state'] not in {'active', 'pending'}):
                raise CustodyError('custody settlement target or owner differs')
            if action == 'retire-unspawned':
                if self._ledger_broken or not self._never_exported(record):
                    raise CustodyError('reservation has no durable never-exported no-spawn evidence')
                if dead_actor is not None:
                    if not isinstance(dead_actor, RetainedChildActor):
                        raise CustodyError('reservation recovery has no retained parent proof')
                    evidence = dead_actor.prove_parent_wait(record['actor'], actor)
                    evidence['admission_id'] = expected_target['admission_id']
                else:
                    if (unspawned is None or unspawned is not self._unspawned_reservation
                            or unspawned.authority is not self or not unspawned.unattempted
                            or unspawned.record != self._semantic(record)):
                        raise CustodyError('reservation has no positive existing-owner no-spawn evidence')
                    evidence = {'producer': 'existing-launch-owner-before-spawn',
                                'admission_id': expected_target['admission_id']}
                status = 'retired'
            else:
                if not isinstance(retained_child, RetainedLaunchChild) or unspawned is not None:
                    raise CustodyError('settlement has no positive retained-child wait evidence')
                evidence = retained_child.prove_wait(self, record)
                status = 'settled'
            ack = {'role': self.role, 'generation': generation, 'owner_epoch': owner_epoch,
                   'actor': requester, 'target': dict(expected_target), 'status': status,
                   'transition_id': transition_id, 'previous_digest': record['digest'],
                   'evidence_digest': _digest_bytes(_canonical(evidence))}
            record['transitions'].append({'transition_id': transition_id, 'intent': intent,
                                          'evidence': evidence, 'ack': ack})
            record['state'] = status
            self._commit(record)
            return dict(ack)

    def bind_pending(self, *, actor: AuthenticatedCleanupActor, admission_id: str,
                     identity: Mapping[str, object], token: Mapping[str, object],
                     generation: int = 1) -> None:
        with self._guard(exclusive=True):
            record = self._read_current()
            self._check_actor(record, actor, generation)
            if record["state"] != "pending" or record["target"] != {"admission_id": admission_id}:
                raise CustodyError("pending launch obligation differs from registration")
            record["target"] = _incarnation(identity, token)
            self._commit(record)

    def bind_target(self, *, actor: AuthenticatedCleanupActor, generation: int,
                    identity: Mapping[str, object], token: Mapping[str, object],
                    final_ready: bool = False) -> dict[str, object]:
        with self._guard(exclusive=True):
            record = self._read_current()
            if record['state'] not in {'pending', 'active'}:
                raise CustodyError('terminal custody cannot be rebound')
            if self._ready_base is not None:
                raise CustodyError("custody readiness baseline cannot be rebound")
            if final_ready and self._anchor_requires_resume and self._admission_resume is None:
                raise CustodyError("custody readiness lacks a completed suspended admission resume")
            self._check_actor(record, actor, generation)
            target = _incarnation(identity, token)
            if any(record["target"].get(k) != target.get(k) for k in ("pid", "birth_id", "uid")):
                raise CustodyError("custody target incarnation changed during exec binding")
            words = token.get("words")
            if not isinstance(words, list) or audit_token_details(words) != {k: token[k] for k in ("pid", "uid", "pidversion", "sha256")}:
                raise CustodyError("custody target kernel token is invalid")
            if self._live_start_anchor is not None:
                if _start_anchor(_darwin_process_start(int(target["pid"]))) != self._live_start_anchor:
                    raise CustodyError("custody monotonic process-start identity changed during exec binding")
                if self._anchor_actor is None:
                    raise CustodyError("live process-start custody scope is unavailable")
                self._anchor_actor.verify()
            record.update(state="active", target={**target, "audit_token_words": words})
            self._commit(record)
            if final_ready:
                if self._retained_record is None or self._ledger_broken:
                    raise CustodyError("custody admission history is unavailable for readiness")
                self._ready_base = self._reference(record)
                self._admission_prefix = json.loads(_canonical(record["transitions"]))
                self._refresh_ledger = []
            return self._reference(record)

    def _check_actor(self, record: Mapping[str, object], actor: AuthenticatedCleanupActor, generation: int) -> None:
        if type(generation) is not int or record["generation"] != generation or record["actor"] != actor.verify():
            raise CustodyError("stale cleanup actor or custody generation")

    def _reference(self, record: Mapping[str, object]) -> dict[str, object]:
        return {"version": ROLE_REFERENCE_VERSION, "scope_root": str(self.root), "role": self.role,
                "generation": record["generation"], "target": {k: v for k, v in record["target"].items() if k != "audit_token_words"}}

    def reference(self) -> dict[str, object]:
        with self._guard(exclusive=False):
            return self._reference(self._read())

    def verify_reference(self, reference: Mapping[str, object], *,
                         expected_actor: Mapping[str, object], owner_epoch: str,
                         identity_provider=None, token_provider=None) -> dict[str, object]:
        """Read-only independent proof; a reference never grants authority.

        A separate consumer can verify the current protected designation, but
        cannot reconstruct retained admission history or acquire signal rights.
        """
        identity_provider = identity_provider or default_process_identity
        token_provider = token_provider or current_process_audit_token
        with self._guard(exclusive=False):
            if self._ledger_broken:
                raise CustodyError("custody retained admission history is unresolved")
            record = self._read_current()
            if (record["state"] != "active" or self._reference(record) != dict(reference)
                    or record["actor"] != dict(expected_actor) or record["owner_epoch"] != owner_epoch):
                raise CustodyError("custody designation differs from selected owner/reference")
            for identity in (record["actor"], reference["target"]):
                observed = identity_provider(int(identity["pid"]))
                if (observed is None
                        or _incarnation(observed, token_provider(int(identity["pid"]))) != dict(identity)):
                    raise CustodyError("custody designation has unavailable or changed kernel identity")
            return self._reference(record)

    def signal(self, *, actor: AuthenticatedCleanupActor, generation: int,
               expected_target: Mapping[str, object], signum: int,
               identity_provider: Callable[[int], Mapping[str, object] | None],
               signal_provider: Callable[[Sequence[int], int], None] = signal_audit_token,
               start_provider: Callable[[int], Mapping[str, object]] | None = None,
               token_provider: Callable[[int], Mapping[str, object]] | None = None) -> dict[str, object]:
        if isinstance(signum, bool) or int(signum) not in _admitted_signals():
            raise CustodyError("custody signal is outside the admitted set")
        # This guard deliberately spans observation AND the kernel signal. Exit
        # waits and RPCs belong outside it; TERM and KILL each reacquire it.
        with self._guard(exclusive=True):
            record = self._read_current()
            self._check_actor(record, actor, generation)
            target = record["target"]
            if record["state"] != "active" or {k: v for k, v in target.items() if k != "audit_token_words"} != dict(expected_target):
                raise CustodyError("custody target is pending, stale or differs from selected role")
            try:
                observed = identity_provider(int(target["pid"]))
            except DarwinProcessExitedError as exc:
                raise _SignalTargetGone("initial custody signal target is no longer live") from exc
            if observed is None and sys.platform == "darwin":
                # This is only an observation. The retained direct-child
                # adapter may reconcile it with a positive Popen wait; an
                # independently observed descendant cannot do so.
                raise _SignalTargetGone(
                    "custody target identity is unknown: absence observation only "
                    f"(stage=signal-identity, candidate_pid={target['pid']}, "
                    f"role={self.role}, generation={generation}, kernel_call_attempted=false)"
                )
            if observed is None or any(observed.get(k) != target[k] for k in ("pid", "birth_id", "uid")):
                raise CustodyError("custody target identity is unknown, absent or replaced")
            if self._live_start_anchor is None:
                # Legacy/handoff records retain their exact token. A PID or a
                # disk anchor is never enough to manufacture refresh authority.
                signal_provider(target["audit_token_words"], signum)
                return self._reference(record)
            read_start = start_provider or _darwin_process_start
            read_token = token_provider or current_process_audit_token
            anchor = self._live_start_anchor
            if self._anchor_actor is not None:
                self._anchor_actor.verify()
            if (self._anchor_actor is None or self._anchor_transition not in record["transitions"]
                    or self._anchor_transition["intent"]["generation"] != generation
                    or self._anchor_transition["intent"]["requester"] != record["actor"]):
                raise CustodyError("live process-start custody scope is unavailable")
            self._anchor_actor.verify()
            for attempt in range(3):
                self._check_actor(record, actor, generation)
                def read_capture_target(reader, phase):
                    try:
                        return reader(int(target["pid"]))
                    except DarwinProcessExitedError as exc:
                        if attempt == 0:
                            raise _SignalCaptureTargetGone(
                                f"initial signal target capture ended (phase={phase}, kernel_call_attempted=false)"
                            ) from exc
                        raise
                before = _start_anchor(read_capture_target(read_start, "start-before"))
                if before != anchor:
                    raise CustodyError("custody monotonic process-start identity was replaced")
                token = dict(read_capture_target(read_token, "token"))
                after = _start_anchor(read_capture_target(read_start, "start-after"))
                if after != anchor:
                    raise CustodyError("custody monotonic process-start identity changed during capture")
                words = _validated_token(token, anchor)
                self._anchor_actor.verify()
                self._check_actor(record, actor, generation)
                refresh = None
                if words != target["audit_token_words"]:
                    previous = {key: value for key, value in target.items() if key != "audit_token_words"}
                    target = {**target, "audit_token_words": words,
                              "audit_token_sha256": token["sha256"], "audit_token_pidversion": token["pidversion"]}
                    record["target"] = target
                    refresh = {
                        "transition_id": "exec-token-" + _digest_bytes(os.urandom(32)),
                        "intent": {"action": "refresh-exec-token", "generation": generation,
                                   "requester": actor.verify(), "previous_target": previous,
                                   "start_anchor": anchor},
                        "ack": {"target": {key: value for key, value in target.items() if key != "audit_token_words"}},
                    }
                    record["transitions"].append(refresh)
                # Even an unchanged captured token has an atomic persistence
                # checkpoint before the kernel call. A failed fsync cannot signal.
                self._commit(record)
                if refresh is not None:
                    ledger = self._refresh_ledger if self._ready_base is not None else self._admission_refresh_ledger
                    ledger.append(json.loads(_canonical(refresh)))
                self._check_actor(record, actor, generation)
                try:
                    signal_provider(words, signum)
                    return self._reference(record)
                except AuditTokenSignalError as exc:
                    if exc.result != errno.ESRCH or attempt == 2:
                        raise
                    # ESRCH is definitely unsuccessful. Recapture under this
                    # same fence; no successful/ambiguous signal is retried.
            raise CustodyError("exact-token signal attempts exhausted")

    def establish_start_anchor(
        self, *, actor: AuthenticatedCleanupActor, generation: int,
        expected_target: Mapping[str, object], authenticated_token: Mapping[str, object],
        require_suspended: bool = True,
        start_provider: Callable[[int], Mapping[str, object]] | None = None,
        token_provider: Callable[[int], Mapping[str, object]] | None = None,
        clock_provider: Callable[[], int] | None = None,
    ) -> None:
        """Establish same-boot refresh authority while the admitted token is live.

        The strict clock fence proves survival beyond the birth tick; this does
        not claim that a timestamp is globally unique. Only this retained object
        can use the anchor, so disk/RPC recovery cannot silently upgrade custody.
        """
        read_start = start_provider or _darwin_process_start
        read_token = token_provider or current_process_audit_token
        read_clock = clock_provider or _darwin_absolute_time
        witness = self._initial_capture_witness
        self._initial_capture_witness = None
        with self._guard(exclusive=True):
            record = self._read_current()
            if self._ready_base is not None:
                raise CustodyError("custody readiness anchor cannot be replaced")
            self._check_actor(record, actor, generation)
            target = record["target"]
            if record["state"] != "active" or self._reference(record)["target"] != dict(expected_target):
                raise CustodyError("start-anchor admission is pending, stale or mismatched")
            if witness is not None and (
                    require_suspended or witness["owner_pid"] != os.getpid()
                    or witness["role"] != self.role or witness["generation"] != generation
                    or witness["actor"] != actor.verify()):
                raise CustodyError("initial capture witness is outside its original owner registration")
            def read_enrollment_target():
                try:
                    return read_start(int(target["pid"]))
                except DarwinProcessExitedError as exc:
                    if witness is not None and not require_suspended:
                        raise _StartAnchorTargetGone("target exited before optional anchor commit") from exc
                    raise
            observed = read_enrollment_target()
            anchor = _start_anchor(observed)
            if witness is not None and anchor != witness["anchor"]:
                raise CustodyError("initial capture witness start changed before enrollment")
            words = _validated_token(authenticated_token, anchor)
            if words != target["audit_token_words"] or (require_suspended and observed.get("status") != 4):
                raise CustodyError("start-anchor lacks the authenticated suspended admission")
            deadline = time.monotonic() + min(self.timeout, 0.1)
            fence = read_clock()
            while fence <= anchor["start_abstime"]:
                if fence < anchor["start_abstime"] or time.monotonic() >= deadline:
                    raise CustodyError("monotonic birth establishment clock fence failed")
                time.sleep(0.001)
                fence = read_clock()
            # The private witness was minted before the ORIGINAL token read,
            # then checked against the identical full start after that read.
            # It is consumed once on this retained authority object. Exec may
            # change token words afterward; only signal() may commit a refresh.
            if witness is not None:
                if fence < witness["clock_fence"]:
                    raise CustodyError("initial capture witness clock moved backwards")
                fresh = dict(authenticated_token)
                fence = witness["clock_fence"]
            else:
                fresh = dict(read_token(int(target["pid"])))
            if (_validated_token(fresh, anchor) != words
                    or _start_anchor(read_enrollment_target()) != anchor):
                raise CustodyError("authenticated process changed across the strict birth fence")
            self._check_actor(record, actor, generation)
            transition = {
                "transition_id": "start-anchor-" + _digest_bytes(os.urandom(32)),
                "intent": {"action": "establish-live-start-anchor", "generation": generation,
                           "requester": actor.verify(), "start_anchor": anchor,
                           "clock_fence": fence, "authenticated_token_sha256": fresh["sha256"]},
                "ack": {"scope": "retained-live-authority-object-only"},
            }
            record["transitions"].append(transition)
            self._commit(record)
            self._live_start_anchor, self._anchor_actor, self._anchor_transition = anchor, actor, transition
            self._anchor_requires_resume = require_suspended
            self._admission_base = dict(expected_target)

    def resume_suspended_target(
        self,
        *,
        actor: AuthenticatedCleanupActor,
        generation: int,
        expected_target: Mapping[str, object],
        release_id: str,
        identity_provider: Callable[[int], Mapping[str, object] | None],
        resume_provider: Callable[[Sequence[int]], None] = _resume_audit_token_once,
    ) -> None:
        """Durably consume one exact launch release before sending SIGCONT."""

        if not isinstance(release_id, str) or not release_id or len(release_id) > 192:
            raise CustodyError("suspended launch release identity is invalid")
        transition_id = "launch-resume-" + release_id
        with self._guard(exclusive=True):
            record = self._read_current()
            if self._ready_base is not None:
                raise CustodyError("suspended admission cannot resume after readiness")
            self._check_actor(record, actor, generation)
            target = record["target"]
            public_target = {key: value for key, value in target.items() if key != "audit_token_words"}
            if record["state"] != "active" or public_target != dict(expected_target):
                raise CustodyError("suspended launch target is pending, stale or differs from selected role")
            observed = identity_provider(int(target["pid"]))
            if observed is None or any(observed.get(key) != target[key] for key in ("pid", "birth_id", "uid")):
                raise CustodyError("suspended launch identity is unknown, absent or replaced")
            if any(item.get("transition_id") == transition_id for item in record["transitions"]):
                raise CustodyError("suspended launch release was already consumed")

            intent = {
                "action": "resume-suspended-launch",
                "release_id": release_id,
                "from_generation": generation,
                "requester": actor.verify(),
                "target_digest": _digest_bytes(_canonical(public_target)),
            }
            transition = {
                "transition_id": transition_id,
                "intent": intent,
                "ack": {"status": "resume-intent-durable"},
            }
            record["transitions"].append(transition)
            self._commit(record)

            try:
                resume_provider(target["audit_token_words"])
            except BaseException as exc:
                self._ledger_broken = True
                # The intent is intentionally not replayable after any signal
                # attempt. A lost/failed syscall therefore remains unresolved.
                failed = self._read_current()
                for item in failed["transitions"]:
                    if item.get("transition_id") == transition_id:
                        item["ack"] = {"status": "resume-call-failed", "error_type": type(exc).__name__}
                        break
                self._commit(failed)
                raise CustodyError("one-shot suspended launch resume failed; custody remains unresolved") from exc

            completed = self._read_current()
            for item in completed["transitions"]:
                if item.get("transition_id") == transition_id:
                    item["ack"] = {"status": "resume-sent"}
                    break
            self._commit(completed)
            self._admission_resume = json.loads(_canonical(next(item for item in completed["transitions"]
                                                               if item["transition_id"] == transition_id)))

    def transfer(self, *, actor: AuthenticatedCleanupActor, next_actor: AuthenticatedCleanupActor,
                 generation: int, transition_id: str, owner_epoch: str,
                 dead_actor: RetainedChildActor | None = None) -> dict[str, object]:
        if not isinstance(transition_id, str) or not transition_id or len(transition_id) > 256:
            raise CustodyError("custody transfer identity is invalid")
        if not isinstance(owner_epoch, str) or not owner_epoch or len(owner_epoch) > 256:
            raise CustodyError("custody owner epoch is invalid")
        with self._guard(exclusive=True):
            # Existing transfer ACK replay remains durable. It neither restores
            # the retained admission mirror nor grants a CR1 token-chain proof.
            record = self._read()
            requester, successor = actor.verify(), next_actor.verify()
            intent = {"transition_id": transition_id, "from_generation": generation,
                      "requester": requester, "next_actor": successor, "owner_epoch": owner_epoch}
            for transition in record["transitions"]:
                if transition["transition_id"] == transition_id:
                    if transition["intent"] != intent:
                        raise CustodyError("custody transfer replay conflicts with durable intent")
                    return dict(transition["ack"])
            record = self._read_current()
            if isinstance(generation, bool) or record["generation"] != generation or record["state"] != "active":
                raise CustodyError("custody transfer has a stale generation or pending target")
            if dead_actor is None:
                if record["actor"] != requester:
                    raise CustodyError("only the designated live actor may relinquish custody")
            else:
                if requester != successor:
                    raise CustodyError("dead-actor recovery must name the authenticated recovering actor")
                dead_actor.prove_exit(record["actor"])
            ack = {"version": DESIGNATION_VERSION, "transition_id": transition_id,
                   "role": self.role, "generation": generation + 1, "owner_epoch": owner_epoch,
                   "actor": successor, "target": {k: v for k, v in record["target"].items() if k != "audit_token_words"}}
            record["transitions"].append({"transition_id": transition_id, "intent": intent, "ack": ack})
            record.update(generation=generation + 1, owner_epoch=owner_epoch, actor=successor)
            self._commit(record)  # Durable commit BEFORE reply/ACK; replay uses this exact record.
            return ack


def default_process_identity(pid: int, *, timeout: float = 1.0) -> dict[str, object] | None:
    """Return identity, None only for proven absence, and raise on observation failure."""

    if timeout <= 0:
        raise CustodyError("process identity observation deadline expired")
    if sys.platform == "darwin":
        rows = _darwin_process_records(pid)
        if not rows:
            return None  # Observation only; never a retained wait/reap.
        row = rows[0]
        if row["status"] == 5:
            raise DarwinProcessExitedError("Darwin process identity observed a zombie; exit/reap remains unproved")
        if row["status"] not in {1, 2, 3, 4}:
            raise CustodyError("Darwin process identity is not live")
        return {"pid": row["pid"], "uid": row["uid"], "parent_pid": row["parent_pid"],
                "birth_id": _ps_lstart_birth(row["start_sec"])}
    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-o", "uid=", "-o", "ppid=", "-o", "lstart=", "-p", str(pid)],
            text=True, capture_output=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CustodyError("process identity observation failed") from exc
    rendered = result.stdout.strip()
    if result.returncode == 1 and not rendered and not result.stderr.strip():
        return None
    if result.returncode != 0:
        raise CustodyError(
            f"process identity observation failed with status {result.returncode}"
        )
    if not rendered:
        raise CustodyError("process identity observation returned no identity")
    parts = rendered.split(None, 2)
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        raise CustodyError("process identity observation was malformed")
    return {"pid": pid, "uid": int(parts[0]), "parent_pid": int(parts[1]), "birth_id": "ps-lstart:" + parts[2]}

PROTOCOL_VERSION = 1
DARWIN_UNIX_SOCKET_PATH_MAX_BYTES = 103
DARWIN_CUSTODY_SOCKET_PARENT = Path("/private/tmp")
AUTHORITY_SCOPE_CLOSED = "admission.closed.json"
PENDING_AUTHORITY_VERSION = "astrid.plan-a.authority-admission-pending/v1"
RESOLVED_AUTHORITY_VERSION = "astrid.plan-a.authority-admission-resolved/v1"


def _create_compact_socket_root() -> Path:
    """Create an owner-only socket directory below Darwin's short temp alias."""

    parent = DARWIN_CUSTODY_SOCKET_PARENT
    try:
        parent_stat = os.lstat(parent)
    except OSError as exc:
        raise CustodyError("custody socket parent is unavailable") from exc
    if (
        not parent.is_absolute()
        or stat.S_ISLNK(parent_stat.st_mode)
        or not stat.S_ISDIR(parent_stat.st_mode)
        or (parent_stat.st_mode & stat.S_IWOTH and not parent_stat.st_mode & stat.S_ISVTX)
    ):
        raise CustodyError("custody socket parent is unsafe")
    root = Path(tempfile.mkdtemp(prefix="runtime-cb-", dir=parent))
    os.chmod(root, 0o700)
    if len(os.fsencode(str(root / "s"))) > DARWIN_UNIX_SOCKET_PATH_MAX_BYTES:
        try:
            root.rmdir()
        finally:
            raise CustodyError("custody socket path exceeds Darwin AF_UNIX limit")
    return root

def _append_owner_jsonl(path: Path, value: Mapping[str, object]) -> None:
    """Durably append one owner-only authority record without replacing peers."""

    if not path.is_absolute() or path.is_symlink():
        raise CustodyError("custody authority journal path is unsafe")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = os.lstat(path.parent)
    if (
        stat.S_ISLNK(parent.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise CustodyError("custody authority journal parent is not owner-only")
    encoded = _canonical(dict(value)) + b"\n"
    if len(encoded) > FRAME_LIMIT:
        raise CustodyError("custody authority journal record exceeds its hard limit")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise CustodyError("custody authority journal is not owner-only")
        if os.write(descriptor, encoded) != len(encoded):
            raise CustodyError("custody authority journal append was incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _acquire_scope_admission(scope_root: Path, *, timeout: float) -> int:
    descriptor = _open_authority_scope_lock(scope_root)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CustodyError("custody authority scope admission timed out")
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        if (Path(scope_root) / AUTHORITY_SCOPE_CLOSED).exists():
            raise CustodyError("custody authority scope is closed")
        return descriptor
    except BaseException:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        raise

class RoleBoundCustodyBroker:
    """One-role broker whose sealed durable ledger is later signal authority."""

    def __init__(
        self,
        *,
        role: str,
        identity_provider: Callable[[int], Mapping[str, object] | None],
        ledger_root: Path,
        timeout: float = 5.0,
        authority_journal: Path | None = None,
        authority_scope_root: Path | None = None,
        owner_epoch: str | None = None,
        launch_parent: Any = None,
        retain_channel: bool = False,
    ) -> None:
        if not role or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in role):
            raise CustodyError("custody role is invalid")
        if sys.platform != "darwin":
            raise CustodyError("Darwin audit-token custody is unavailable")
        if type(retain_channel) is not bool or (retain_channel and role not in {'command', 'engine'}):
            raise CustodyError('post-exec custody role has no matched pipe consumer')
        self.launch_parent = launch_parent
        self.retain_channel = retain_channel
        self.channel: socket.socket | None = None
        self.operation_channel: _PipeChannel | None = None
        self.child_pipe_binding: list[dict[str, int]] = []
        self.hosted_launch = None
        self.role = role
        self.identity_provider = identity_provider
        self.timeout = timeout
        self.authority_journal = (
            Path(os.path.abspath(authority_journal))
            if authority_journal is not None else None
        )
        self.authority_scope_root = (
            Path(os.path.abspath(authority_scope_root))
            if authority_scope_root is not None else None
        )
        if (self.authority_journal is None) != (self.authority_scope_root is None):
            raise CustodyError("custody authority journal and scope must be configured together")
        self._authority_scope_descriptor: int | None = None
        self._authority_scope_guard = threading.Lock()
        self.run_id = _digest_bytes(os.urandom(32))
        ledger_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(ledger_root, 0o700)
        self.root = ledger_root
        self.journal_path = ledger_root / "custody.journal.jsonl"
        self.ledger_path = ledger_root / "custody.ledger.json"
        self.cleanup_actor = AuthenticatedCleanupActor.current()
        self.role_authority = RoleCustodyAuthority(self.authority_scope_root or (ledger_root / "authority"), role, timeout=timeout)
        self.owner_epoch = owner_epoch or self.run_id
        self.role_authority.reserve_before_spawn(actor=self.cleanup_actor, owner_epoch=self.owner_epoch,
                                                 admission_id=self.run_id)
        self.custody_generation = self.role_authority.reference()["generation"]
        self._retained_launch_child: RetainedLaunchChild | None = None
        self.socket_root = _create_compact_socket_root()
        self.socket_path = self.socket_root / "s"
        listener: socket.socket | None = None
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(1)
            listener.settimeout(timeout)
        except BaseException:
            if listener is not None:
                listener.close()
            self.socket_path.unlink(missing_ok=True)
            try:
                self.socket_root.rmdir()
            except OSError:
                pass
            if self._authority_scope_descriptor is not None:
                fcntl.flock(self._authority_scope_descriptor, fcntl.LOCK_UN)
                os.close(self._authority_scope_descriptor)
                self._authority_scope_descriptor = None
            raise
        self.listener = listener
        try:
            self._authority_scope_descriptor = (
                _acquire_scope_admission(self.authority_scope_root, timeout=timeout)
                if self.authority_scope_root is not None else None
            )
        except BaseException:
            self.listener.close()
            self.socket_path.unlink(missing_ok=True)
            try:
                self.socket_root.rmdir()
            except OSError:
                pass
            raise
        self.sequence = 0
        self.chain_head: str | None = None
        self.state = "accepting"
        self.registration: dict[str, object] | None = None
        self.ack: dict[str, object] | None = None
        self.error: BaseException | None = None
        self._post_exec_authority_validated = False
        self._release_attempted = False
        self._release_sent = False
        self._cleanup_deadline: float | None = None
        self._observation_phase = "accept"
        self._launch_pid: int | None = None
        self._ready = threading.Event()
        if retain_channel:
            descriptors = []
            try:
                request_read, request_write = os.pipe()
                descriptors.extend((request_read, request_write))
                reply_read, reply_write = os.pipe()
                descriptors.extend((reply_read, reply_write))
                self.operation_channel = _PipeChannel(request_read, reply_write)
                self.child_pipe_binding = [_pipe_identity(reply_read, os.O_RDONLY),
                                           _pipe_identity(request_write, os.O_WRONLY)]
            except BaseException:
                self.operation_channel = None
                self.child_pipe_binding = []
                for fd in descriptors:
                    os.close(fd)
                self.abort_before_spawn()
                raise
        self._thread = threading.Thread(target=self._serve, daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self.close_child_pipes()
            if self.operation_channel is not None:
                self.operation_channel.close()
            self.listener.close()
            self.socket_path.unlink(missing_ok=True)
            try:
                self.socket_root.rmdir()
            except OSError:
                pass
            self._release_authority_scope()
            raise

    def _release_authority_scope(self) -> None:
        with self._authority_scope_guard:
            descriptor = self._authority_scope_descriptor
            self._authority_scope_descriptor = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def abort_before_spawn(self) -> None:
        """Release an admission that never reached a workload Popen call."""

        if self.registration is not None:
            raise CustodyError("registered custody cannot use pre-spawn abort")
        self.role_authority.settle(
            actor=self.cleanup_actor, generation=self.custody_generation, owner_epoch=self.owner_epoch,
            expected_target={'admission_id': self.run_id}, transition_id='retire-' + self.run_id,
            unspawned=self.role_authority._unspawned_reservation)
        self.listener.close()
        self.socket_path.unlink(missing_ok=True)
        try:
            self.socket_root.rmdir()
        except OSError:
            pass
        self._release_authority_scope()
        self.close_child_pipes()
        if self.operation_channel is not None:
            self.operation_channel.close()

        if getattr(self, 'hosted_launch', None) is not None:
            self.hosted_launch.close_children()

    def retain_launch_child(self, process: subprocess.Popen) -> None:
        self._retained_launch_child = self.role_authority.retain_child(
            actor=self.cleanup_actor, generation=self.custody_generation,
            child=process, identity_provider=self.identity_provider)

    def settle_launch(self) -> dict[str, object]:
        """Called by the owner after its real terminal child wait."""
        if not hasattr(self, '_settlement_target'):
            self._settlement_target = self.role_authority.reference()['target']
        return self.role_authority.settle(
            actor=self.cleanup_actor, generation=self.custody_generation, owner_epoch=self.owner_epoch,
            expected_target=self._settlement_target, transition_id='settle-' + self.run_id,
            retained_child=self._retained_launch_child)

    @property
    def child_pipe_fds(self) -> tuple[int, ...]:
        if self.child_pipe_binding and not getattr(self, '_child_pipes_closed', False):
            self.role_authority.begin_spawn()
        return tuple(item['fd'] for item in self.child_pipe_binding)

    def close_child_pipes(self) -> None:
        if not getattr(self, '_child_pipes_closed', False):
            self._child_pipes_closed = True
            # Closing private endpoints is not an export (including after retirement).
            for item in self.child_pipe_binding:
                os.close(item['fd'])

    def retained_pipe_actor(self) -> AuthenticatedCleanupActor:
        if ((self.operation_channel is None and getattr(self, 'child_bridge_binding', None) is None) or not self._release_sent
                or not self._post_exec_authority_validated or self.registration is None):
            raise CustodyError('pipe actor lacks authenticated retained registration')
        expected = self.registration['identity']
        pid = int(expected['pid'])
        anchor = self.registration.get('bootstrap_start_anchor')
        if not isinstance(anchor, dict):
            raise CustodyError('pipe actor lacks its authenticated bootstrap birth fence')
        actor = AuthenticatedCleanupActor(lambda: _bootstrap_anchored_token(pid, anchor), self.identity_provider)
        observed = actor.verify()
        if any(observed.get(key) != expected.get(key) for key in ('pid', 'uid', 'birth_id')):
            raise CustodyError('pipe actor differs from the registered incarnation')
        return actor

    def create_hosted_launch(self, preparation):
        if self.hosted_launch is not None:
            raise CustodyError("hosted endpoints already allocated")
        try:
            self.hosted_launch = hosted_contract.HostedLaunch(self, preparation)
            return self.hosted_launch
        except BaseException:
            self.abort_before_spawn()
            raise

    def child_environment(self, argv: Sequence[str], *, start_new_session: bool) -> dict[str, str]:
        candidate = Path(argv[0])
        if not candidate.is_absolute():
            located = shutil.which(argv[0])
            if located is None:
                raise CustodyError("custodied executable is unavailable")
            candidate = Path(located)
        lexical_executable = str(Path(os.path.abspath(candidate)))
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise CustodyError("custodied executable is not executable")
        # Preserve the validated lexical venv executable for exec. Resolving a
        # venv symlink here would silently leave its installed environment.
        normalized = [lexical_executable, *list(argv[1:])]
        if self.role not in {'command', 'engine', 'engine_listener', 'host', 'relay'}:
            raise CustodyError('custody role has no matched exec consumer')
        hosted_launch = getattr(self, 'hosted_launch', None)
        if self.role in {'host', 'relay'} and hosted_launch is None:
            raise CustodyError("host/relay launch lacks owned authenticated endpoints")
        if hosted_launch is not None and hosted_launch.value['argv_digest'] != _digest_bytes(_canonical(normalized)):
            raise CustodyError("hosted executable selection changed")
        environment = {
            "ASTRID_RUNTIME_CUSTODY_SOCKET": str(self.socket_path),
            "ASTRID_RUNTIME_CUSTODY_RUN_ID": self.run_id,
            "ASTRID_RUNTIME_CUSTODY_ROLE": self.role,
            "ASTRID_RUNTIME_CUSTODY_START_SESSION": "1" if start_new_session else "0",
            "ASTRID_RUNTIME_CUSTODY_TIMEOUT_SECONDS": repr(self.timeout),
            "ASTRID_RUNTIME_CUSTODY_TARGET_B64": base64.b64encode(
                _canonical(normalized)
            ).decode("ascii"),
            **({_BOOTSTRAP_PIPES: _canonical(self.child_pipe_binding).decode('ascii')}
               if self.child_pipe_binding else {}),
            **({_BOOTSTRAP_CHILD_BRIDGE: _canonical(self.child_bridge_binding).decode('ascii')}
               if getattr(self, 'child_bridge_binding', None) is not None else {}),
        }
        # Exporting bootstrap authority permits a caller to create a child.
        # A later registration absence cannot prove that no creation occurred.
        self.role_authority.begin_spawn()
        return environment

    def wait_until_sealed(self) -> None:
        diagnostic = lambda: (f"stage={getattr(self, '_observation_phase', 'unknown')}, "
                              f"launch_pid={getattr(self, '_launch_pid', None)}")
        if not self._ready.wait(self.timeout):
            raise CustodyError(f"custody registration timed out ({diagnostic()})")
        self._thread.join(timeout=self.timeout)
        if self.error is not None:
            raise CustodyError(f"custody registration failed ({diagnostic()}): {self.error}") from self.error
        if (
            self._thread.is_alive()
            or self.state not in {"sealed", "ready-bound"}
            or self.registration is None
        ):
            raise CustodyError("custody admission did not seal")

    def set_cleanup_deadline(self, deadline: float) -> None:
        self._cleanup_deadline = deadline

    def _observe_registered_identity(self, pid: int) -> Mapping[str, object] | None:
        if self.identity_provider is default_process_identity:
            remaining = (
                1.0 if self._cleanup_deadline is None
                else self._cleanup_deadline - time.monotonic()
            )
            return default_process_identity(pid, timeout=min(1.0, remaining))
        return self.identity_provider(pid)

    def signal(self, signum: int, *, expected_pid: int) -> None:
        """Signal only the sealed role and exact process incarnation."""

        if int(signum) not in _admitted_signals():
            raise CustodyError("custody signal is outside the admitted set")
        if self.state not in {"sealed", "ready-bound"} or self.registration is None:
            raise CustodyError("custody admission is not sealed")
        identity = self.registration.get("identity")
        if not isinstance(identity, dict) or identity.get("pid") != expected_pid:
            raise CustodyError("registered custody PID differs from the process handle")
        self.role_authority.signal(
            actor=self.cleanup_actor, generation=self.custody_generation,
            expected_target=self.role_authority.reference()["target"], signum=signum,
            identity_provider=self._observe_registered_identity,
        )

    def signal_failed_admission(self, signum: int, *, expected_pid: int) -> None:
        """Clean up only a failed admission with authenticated post-exec authority.

        A persistence or final-seal failure must not discard the live child.  This
        path does not admit that child: it is available only after the broker has
        authenticated the post-exec audit token and current process incarnation.
        """

        if int(signum) not in _admitted_signals():
            raise CustodyError("custody signal is outside the admitted set")
        if (
            self.error is None
            or not self._post_exec_authority_validated
            or self.registration is None
        ):
            raise CustodyError("failed admission has no validated post-exec authority")
        identity = self.registration.get("identity")
        if not isinstance(identity, dict) or identity.get("pid") != expected_pid:
            raise CustodyError("registered custody PID differs from the process handle")
        self.role_authority.signal(
            actor=self.cleanup_actor, generation=self.custody_generation,
            expected_target=self.role_authority.reference()["target"], signum=signum,
            identity_provider=self._observe_registered_identity,
        )

    def release_suspended_launch(self, *, expected_pid: int, process: Any = None) -> None:
        """Release the exact suspended image once, after its observer is ready."""

        if self.state != "sealed" or self.registration is None:
            raise CustodyError("suspended launch cannot release before durable sealing")
        identity = self.registration.get("identity")
        if not isinstance(identity, Mapping) or identity.get("pid") != expected_pid:
            raise CustodyError("suspended launch differs from the retained child")
        if self._release_attempted:
            raise CustodyError("suspended launch release was already attempted")
        self._release_attempted = True
        try:
            self._persist("launch_release_intent")
            reference = self.role_authority.reference()
            self.role_authority.resume_suspended_target(
                actor=self.cleanup_actor,
                generation=self.custody_generation,
                expected_target=reference["target"],
                release_id=self.run_id,
                identity_provider=self._observe_registered_identity,
            )
            self._release_sent = True
            # Launch success does not require a token that remains valid across
            # later launcher execs. Each destructive call captures its own token.
            self._persist("launch_release_sent")
        except BaseException as exc:
            self.role_authority.break_token_chain()
            self.error = exc
            raise

    def bind_resumed_token(self, *, expected_pid: int, process: Any = None) -> bool:
        """Refresh signal authority after Darwin advances pidversion on resume."""

        if not self._release_sent or self.registration is None:
            raise CustodyError("post-resume token cannot bind before launch release")
        pinned = self.registration.get("identity")
        expected = {key: pinned.get(key) for key in ("pid", "birth_id", "uid")} if isinstance(pinned, Mapping) else {}
        if expected_pid != expected.get("pid"):
            raise CustodyError("post-resume token target differs from the retained child")
        deadline = time.monotonic() + self.timeout
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                self._persist("launch_exited_before_post_resume_token")
                return False
            try:
                before = self._observe_registered_identity(expected_pid)
                if before is None:
                    if process is None or process.poll() is not None:
                        self._persist("launch_exited_before_post_resume_token")
                        return False
                    raise CustodyError("post-resume process identity is unavailable")
                if {key: before.get(key) for key in expected} != expected:
                    raise CustodyError("post-resume process incarnation changed")
                token = current_process_audit_token(expected_pid)
                after = self._observe_registered_identity(expected_pid)
                if after is None or {key: after.get(key) for key in expected} != expected:
                    raise CustodyError("post-resume process incarnation changed while binding")
                words = token.get("words")
                if (
                    token.get("pid") != expected_pid
                    or token.get("uid") != expected.get("uid")
                    or not isinstance(token.get("pidversion"), int)
                    or not isinstance(token.get("sha256"), str)
                    or not isinstance(words, list)
                    or audit_token_details(words) != {
                        key: token[key] for key in ("pid", "pidversion", "uid", "sha256")
                    }
                ):
                    raise CustodyError("post-resume Runtime audit token is invalid")
                self.registration.update({
                    "suspended_audit_token_sha256": self.registration.get("audit_token_sha256"),
                    "suspended_pidversion": self.registration.get("audit_token_pidversion"),
                    "identity": dict(after),
                    "audit_token_words": list(words),
                    "audit_token_sha256": token["sha256"],
                    "audit_token_pidversion": token["pidversion"],
                })
                self.role_authority.bind_target(
                    actor=self.cleanup_actor, generation=self.custody_generation,
                    identity=after, token=token,
                )
                self._persist("launch_post_resume_token_bound")
                return True
            except BaseException as exc:
                last_error = exc
                if process is not None and process.poll() is not None:
                    self._persist("launch_exited_before_post_resume_token")
                    return False
                time.sleep(0.005)
        raise CustodyError("post-resume exact audit-token binding timed out") from last_error

    def bind_ready_token(
        self,
        *,
        expected_pid: int,
        expected_identity: Mapping[str, object],
        token_provider: Callable[[int], Mapping[str, object]] = current_process_audit_token,
    ) -> None:
        """Replace the provisional post-exec token with the final ready owner."""

        if self.state != "sealed" or self.registration is None:
            raise CustodyError("custody admission is not provisionally sealed")
        pinned = self.registration.get("identity")
        expected = {key: expected_identity.get(key) for key in ("pid", "birth_id", "uid")}
        if (
            not isinstance(pinned, Mapping)
            or expected_pid != expected.get("pid")
            or {key: pinned.get(key) for key in expected} != expected
        ):
            raise CustodyError("final-ready Runtime identity differs from provisional custody")
        before = self.identity_provider(expected_pid)
        if before is None or {key: before.get(key) for key in expected} != expected:
            raise CustodyError("final-ready Runtime incarnation is unavailable")
        token = dict(token_provider(expected_pid))
        after = self.identity_provider(expected_pid)
        if after is None or {key: after.get(key) for key in expected} != expected:
            raise CustodyError("final-ready Runtime incarnation changed while binding")
        words = token.get("words")
        if (
            token.get("pid") != expected_pid
            or token.get("uid") != expected.get("uid")
            or not isinstance(token.get("pidversion"), int)
            or not isinstance(token.get("sha256"), str)
            or not isinstance(words, list)
            or audit_token_details(words) != {
                key: token[key] for key in ("pid", "pidversion", "uid", "sha256")
            }
        ):
            raise CustodyError("final-ready Runtime audit token is invalid")
        self.registration.update({
            "post_exec_audit_token_sha256": self.registration.get("audit_token_sha256"),
            "post_exec_pidversion": self.registration.get("audit_token_pidversion"),
            "identity": dict(after),
            "audit_token_words": list(words),
            "audit_token_sha256": token["sha256"],
            "audit_token_pidversion": token["pidversion"],
        })
        try:
            self.role_authority.bind_target(actor=self.cleanup_actor, generation=self.custody_generation,
                                            identity=after, token=token, final_ready=True)
            self.sequence = 3
            self.state = "ready-bound"
            self._persist("readiness_token_bound")
            self.role_authority.commit_ready()
        except BaseException:
            self.role_authority.break_token_chain()
            raise

    def _persist(self, event_name: str) -> None:
        try:
            self._persist_unfenced(event_name)
        except BaseException:
            self.role_authority.break_token_chain()
            raise

    def _persist_unfenced(self, event_name: str) -> None:
        snapshot = {
            "version": PROTOCOL_VERSION,
            "run_id": self.run_id,
            "role": self.role,
            "state": self.state,
            "sequence": self.sequence,
            "registration": self.registration,
            "ack": self.ack,
        }
        event: dict[str, object] = {
            "version": PROTOCOL_VERSION,
            "event": event_name,
            "sequence": self.sequence,
            "predecessor_digest": self.chain_head,
            "snapshot_digest": _digest_bytes(_canonical(snapshot)),
            "snapshot": snapshot,
        }
        event["event_digest"] = _digest_bytes(_canonical(event))
        descriptor = os.open(
            self.journal_path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, _canonical(event) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.chain_head = str(event["event_digest"])
        _atomic_owner_json(self.ledger_path, {**snapshot, "chain_digest": self.chain_head})

    def _serve(self) -> None:
        connection: socket.socket | None = None
        try:
            connection, _ = self.listener.accept()
            self.channel = connection
            connection.settimeout(self.timeout)
            self._observation_phase = "pre-exec-frame"
            frame = _read_frame(connection)
            required = {"version", "command", "run_id", "role", "pid", "ppid", "argv_digest"}
            if getattr(self, 'hosted_launch', None) is not None:
                required |= {'hosted_digest', 'hosted_process'}
                if (frame.get('hosted_digest') != _digest_bytes(_canonical(self.hosted_launch.value))
                        or frame.get('argv_digest') != self.hosted_launch.value['argv_digest']
                        or frame.get('hosted_process') != hosted_contract.incarnation(frame.get('pid'))):
                    raise CustodyError("hosted role, generation, launch or incarnation differs")
            if self.child_pipe_binding:
                required.add('pipes')
            if getattr(self, 'child_bridge_binding', None) is not None:
                required.add('child_bridge')
            if (
                set(frame) != required
                or frame["version"] != PROTOCOL_VERSION
                or frame["command"] != "register_pre_exec"
                or frame["run_id"] != self.run_id
                or frame["role"] != self.role
                or frame["ppid"] != int((self.launch_parent or self.cleanup_actor).verify()["pid"])
                or isinstance(frame["pid"], bool)
                or not isinstance(frame["pid"], int)
                or frame["pid"] <= 0
                or frame.get('pipes', []) != self.child_pipe_binding
                or frame.get('child_bridge') != getattr(self, 'child_bridge_binding', None)
            ):
                raise CustodyError("custody registration frame is invalid")
            self._observation_phase = "peer-token"
            pre = _peer_token(connection)
            if pre["pid"] != frame["pid"] or pre["uid"] != os.getuid():
                raise CustodyError("custody registration kernel identity differs")
            self._observation_phase = "initial-identity"
            before = self.identity_provider(int(frame["pid"]))
            if before is None or before.get("parent_pid") != frame["ppid"]:
                raise CustodyError("pre-exec process identity/parent is unavailable or mismatched")
            self.sequence = 1
            self.registration = {
                "pid": frame["pid"], "identity": dict(before),
                "argv_digest": frame["argv_digest"],
                "pre_exec_audit_token_sha256": pre["sha256"],
                "pre_exec_pidversion": pre["pidversion"],
                "audit_token_words": pre["words"],
            }
            # Pending cleanup ownership is durable before registration can ACK
            # and let exec proceed. It conveys no destructive signal authority.
            self._observation_phase = "pending-checkpoint"
            self.role_authority.bind_pending(actor=self.cleanup_actor, admission_id=self.run_id,
                                             identity=before, token=pre, generation=self.custody_generation)
            self._persist("registration_pre_exec")
            self.ack = {
                "version": PROTOCOL_VERSION, "status": "registered",
                "run_id": self.run_id, "role": self.role, "pid": frame["pid"],
                "registration_digest": _digest_bytes(_canonical(frame)),
                "ledger_state_digest": self.chain_head,
            }
            self._persist("registration_ack_checkpoint")
            if self.authority_journal is not None:
                identity = dict(self.registration["identity"])  # type: ignore[arg-type]
                _append_owner_jsonl(self.authority_journal, {
                    "version": PENDING_AUTHORITY_VERSION,
                    "admission_id": self.run_id,
                    "role": self.role,
                    "pid": int(frame["pid"]),
                    "identity": identity,
                    "binding": {
                        "admission_id": self.run_id,
                        "role": self.role,
                        "pid": int(frame["pid"]),
                        "birth_id": identity.get("birth_id"),
                        "uid": identity.get("uid"),
                        "state": "pre-exec-ack-pending",
                        "registration_before_exec": True,
                        "authenticated_signal_authority": False,
                    },
                })
            self._observation_phase = "pre-exec-task-pin"
            bootstrap_anchor = _authenticated_bootstrap_anchor(int(frame['pid']), pre)
            self.registration['bootstrap_start_anchor'] = bootstrap_anchor
            self._persist('registration_bootstrap_anchor')
            self._observation_phase = "ack-send"
            _send_frame(connection, self.ack)
            self._observation_phase = "post-exec-token"
            deadline = time.monotonic() + self.timeout
            post: dict[str, object] | None = None
            while time.monotonic() < deadline:
                # The socket closes in trusted bootstrap. Fresh task-token
                # capture stays inside its authenticated exec-stable birth fence.
                candidate = _bootstrap_anchored_token(int(frame['pid']), bootstrap_anchor)
                if candidate["pidversion"] != pre["pidversion"]:
                    post = candidate
                    break
                time.sleep(0.005)
            if post is None or post["pid"] != frame["pid"] or post["uid"] != os.getuid():
                raise CustodyError("post-exec audit token did not bind the registered process")
            self._observation_phase = "post-exec-identity"
            after = self.identity_provider(int(frame["pid"]))
            if after is None or any(after.get(k) != before.get(k) for k in ("pid", "birth_id", "uid")):
                raise CustodyError("pre/post-exec process incarnation differs")
            self.registration.update({
                "identity": dict(after), "audit_token_words": post["words"],
                "audit_token_sha256": post["sha256"],
                "audit_token_pidversion": post["pidversion"],
            })
            self._observation_phase = "target-bind"
            self.role_authority.bind_target(actor=self.cleanup_actor, generation=self.custody_generation,
                                            identity=after, token=post)
            self._observation_phase = "start-anchor"
            self.role_authority.establish_start_anchor(
                actor=self.cleanup_actor, generation=self.custody_generation,
                expected_target=self.role_authority.reference()["target"],
                authenticated_token=post,
            )
            self._post_exec_authority_validated = True
            if self.authority_journal is not None:
                identity = dict(self.registration["identity"])  # type: ignore[arg-type]
                _append_owner_jsonl(self.authority_journal, {
                    "version": "astrid.plan-a.retained-audit-authority/v1",
                    "role": self.role,
                    "pid": int(frame["pid"]),
                    "identity": identity,
                    "audit_token_words": list(self.registration["audit_token_words"]),  # type: ignore[arg-type]
                    "binding": {
                        "admission_id": self.run_id,
                        "role": self.role,
                        "pid": int(frame["pid"]),
                        "birth_id": identity.get("birth_id"),
                        "uid": identity.get("uid"),
                        "state": "post-exec-authority-validated",
                        "registration_before_exec": True,
                        "pre_post_exec_incarnation_bound": True,
                        "signal_primitive": "proc_signal_with_audittoken",
                    },
                })
                _append_owner_jsonl(self.authority_journal, {
                    "version": RESOLVED_AUTHORITY_VERSION,
                    "admission_id": self.run_id,
                    "resolution": "validated-authority-exported",
                    "role": self.role,
                    "pid": int(frame["pid"]),
                    "identity": identity,
                })
            self.sequence = 2
            self._persist("registration_post_exec")
            self._observation_phase = "seal"
            self.state = "sealed"
            self._persist("admission_sealed")
        except BaseException as exc:
            self.error = exc
        finally:
            self._ready.set()
            if connection is not None:
                connection.close()
            if self.error is not None and self.operation_channel is not None:
                self.operation_channel.close()
            self.listener.close()
            self.socket_path.unlink(missing_ok=True)
            try:
                self.socket_root.rmdir()
            except OSError:
                pass
            self._release_authority_scope()

def custody_wrapper_argv(executable: str) -> list[str]:
    # The stdlib-only control phase must register before a workload's
    # sitecustomize network hook can deny the private AF_UNIX channel. These
    # flags apply only here: SETEXEC retains the original target argv/env,
    # so normal target startup still installs its admitted workload policy.
    return [executable, "-I", "-S", "-u", str(Path(__file__).absolute()), "--custody-exec"]

def child_exec_from_environment() -> int:
    encoded = os.environ.get("ASTRID_RUNTIME_CUSTODY_TARGET_B64", "")
    try:
        argv = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError("custody target argv is invalid") from exc
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or "\0" in v for v in argv):
        raise CustodyError("custody target argv is invalid")
    role = os.environ.get('ASTRID_RUNTIME_CUSTODY_ROLE')
    if role not in {'command', 'engine', 'engine_listener', 'host', 'relay'}:
        raise CustodyError('custody role has no matched exec consumer')
    command_channel = os.environ.get('ASTRID_COMMAND_CUSTODY_ENABLED', '') == '1'
    encoded_pipes = os.environ.get(_BOOTSTRAP_PIPES)
    pipe_binding = json.loads(encoded_pipes) if encoded_pipes is not None else []
    pipe_fds = _validate_pipe_binding(pipe_binding) if encoded_pipes is not None else ()
    child_bridge = json.loads(os.environ[_BOOTSTRAP_CHILD_BRIDGE]) if _BOOTSTRAP_CHILD_BRIDGE in os.environ else None
    bridge_fds = ()
    if _CHILD_BRIDGE_CONSUMER in os.environ:
        raise CustodyError('child bridge consumer cannot select bootstrap authority')
    if child_bridge is not None:
        if pipe_fds or command_channel:
            raise CustodyError('child bridge cannot inherit custody control pipes')
        bridge_fds = _validate_child_bridge_binding(child_bridge, role=role, argv=argv,
            run_id=os.environ.get('ASTRID_RUNTIME_CUSTODY_RUN_ID'))
    if (bool(pipe_fds) != (role == 'engine' or command_channel)
            or (command_channel and role != 'command')
            or _COMMAND_INVENTORY_FD in os.environ or 'ASTRID_ENGINE_CONTROL_FD' in os.environ):
        raise CustodyError('post-exec pipe selection is absent or unsupported')
    hosted = None
    if os.environ.get('ASTRID_RUNTIME_CUSTODY_ROLE') in {'host', 'relay'}:
        hosted = hosted_contract.bootstrap(os.environ, argv)
    elif any(key in os.environ for key in (hosted_contract.FD_KEY, hosted_contract.DIGEST_KEY,
                                           hosted_contract.CONSUMER_FD, hosted_contract.CONSUMER_DIGEST)):
        raise CustodyError('hosted descriptors cannot enter another role')
    # Session creation changes Darwin's audit-token pidversion.  Complete it
    # before the broker samples the pre-exec token.  The target may perform
    # further launcher execs, so this provisional token is rebound only after
    # Runtime readiness by the parent that retains the direct-child handle.
    if os.environ.get("ASTRID_RUNTIME_CUSTODY_START_SESSION") == "1":
        os.setsid()
    try:
        timeout = float(os.environ.get("ASTRID_RUNTIME_CUSTODY_TIMEOUT_SECONDS", "5"))
    except ValueError as exc:
        raise CustodyError("custody registration timeout is invalid") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise CustodyError("custody registration timeout is invalid")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # The broker durably checkpoints the pre-exec registration before its ACK.
    # Use the broker's own bounded timeout so a slow fsync cannot make the child
    # abandon a registration the broker is still entitled to complete.
    connection.settimeout(timeout)
    connection.connect(os.environ.get("ASTRID_RUNTIME_CUSTODY_SOCKET", ""))
    frame = {
        "version": PROTOCOL_VERSION, "command": "register_pre_exec",
        "run_id": os.environ.get("ASTRID_RUNTIME_CUSTODY_RUN_ID", ""),
        "role": os.environ.get("ASTRID_RUNTIME_CUSTODY_ROLE", ""),
        "pid": os.getpid(), "ppid": os.getppid(),
        "argv_digest": _digest_bytes(_canonical(argv)),
        **({'pipes': pipe_binding} if pipe_fds else {}),
        **({'child_bridge': child_bridge} if child_bridge is not None else {}),
        **({'hosted_digest': _digest_bytes(_canonical(hosted[0])), 'hosted_process': hosted[1]} if hosted else {}),
    }
    _send_frame(connection, frame)
    ack = _read_frame(connection)
    if (
        ack.get("status") != "registered" or ack.get("run_id") != frame["run_id"]
        or ack.get("role") != frame["role"] or ack.get("pid") != os.getpid()
        or ack.get("registration_digest") != _digest_bytes(_canonical(frame))
    ):
        raise CustodyError("custody registration acknowledgement is invalid")
    # Registration ends in trusted bootstrap. Only the matched trusted
    # host/relay endpoints or authenticated workload pipes can survive admission.
    connection.close()
    os.environ.pop('ASTRID_COMMAND_CUSTODY_ENABLED', None)
    for name in tuple(os.environ):
        if name.startswith("ASTRID_RUNTIME_CUSTODY_"):
            os.environ.pop(name, None)
    if pipe_fds:
        name = 'ASTRID_ENGINE_CONTROL_FD' if role == 'engine' else _COMMAND_INVENTORY_FD
        os.environ[name] = _canonical(pipe_binding).decode('ascii')
    if child_bridge is not None:
        os.environ[_CHILD_BRIDGE_CONSUMER] = _canonical(child_bridge).decode('ascii')
    hosted_proof = None
    approved = pipe_fds + bridge_fds
    if hosted:
        hosted_proof, selected = hosted_contract.publish_consumer(*hosted)
        os.environ.update(selected)
        approved = tuple(hosted_contract.validate(hosted[0], role=role)) + (hosted_proof.fileno(),)
    _close_workload_descriptors(approved)
    for fd in pipe_fds + bridge_fds:
        os.set_inheritable(fd, True)
    _posix_spawn_setexec_suspended(argv, dict(os.environ))
    raise AssertionError("suspended SETEXEC returned")


# Private command-owner inventory reads. Frames never authorize arbitrary argv.
_COMMAND_INVENTORY_VERSION = 'astrid.command-inventory/v1'
_COMMAND_INVENTORY_FD = 'ASTRID_COMMAND_CONTROL_FD'
_COMMAND_GIT_OPERATIONS = {
    'revision': ('rev-parse', 'HEAD'),
    'clean_status': ('status', '--porcelain=v1'),
    'repository_root': ('rev-parse', '--show-toplevel'),
    'tracked_files': ('ls-files', '--full-name', '-z'),
}
_COMMAND_CLIENTS: dict[tuple[int, str], Any] = {}
_COMMAND_CLIENTS_LOCK = threading.Lock()


def _inventory_launch_policy(environment: Mapping[str, str], cwd: str) -> dict[str, Any] | None:
    """Pin declared managed roots, not a cached verification of their contents."""
    import shutil
    from astrid.core.pack import source_setup as sources
    configured = environment.get(sources.SOURCE_STATE_ENV)
    state_home = Path(environment.get('XDG_STATE_HOME', str(Path(environment.get('HOME', str(Path.home()))) / '.local/state')))
    path = Path(configured) if configured else state_home / 'astrid/default/pack-sources.json'
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise CustodyError('command inventory state is unavailable')
    path = path.resolve(strict=True)
    raw = path.read_bytes()
    state = json.loads(raw)
    if not isinstance(state, dict) or state.get('version') != sources.SOURCE_STATE_VERSION:
        raise CustodyError('command inventory state is malformed')
    roots = {}
    for key, value in state.get('active', {}).items():
        if key in state.get('disabled', []):
            continue
        record = sources._record_from_mapping(value)
        if record.pack_id != key:
            raise CustodyError('command inventory source key differs')
        declaration = sources.SourceDeclaration(record.pack_id, record.repository, record.revision,
                                                record.pack_subpath, record.manifest_sha256, record.tree_sha256)
        checkout = sources.next_parent_checkout(record.pack_root, declaration)
        if not checkout.is_dir() or checkout.is_symlink() or checkout.resolve(strict=True) != checkout:
            raise CustodyError('command inventory checkout is aliased')
        roots.setdefault(str(checkout), []).append(str(record.pack_root))
    if not roots:
        return None
    executable = shutil.which('git', path=environment.get('PATH'))
    if executable is None:
        raise CustodyError('command inventory Git executable is unavailable')
    git = Path(executable).resolve(strict=True)
    if not git.is_file() or not os.access(git, os.X_OK):
        raise CustodyError('command inventory Git executable is unavailable')
    return {'state_path': str(path), 'state_sha256': _digest_bytes(raw), 'roots': roots,
            'git': str(git), 'git_sha256': _digest_bytes(git.read_bytes()),
            'cwd': str(Path(cwd).resolve(strict=True)), 'environment': dict(environment)}


class _InventoryChannelEOF(CustodyError):
    pass


def _read_inventory_frame(connection: socket.socket) -> dict[str, Any]:
    # Streaming replies can arrive together. Consume exactly one bounded frame;
    # the existing one-request custody registration parser stays unchanged.
    raw = bytearray()
    while True:
        peek = connection.recv(min(4096, FRAME_LIMIT + 1 - len(raw)), socket.MSG_PEEK)
        if not peek:
            if not raw:
                raise _InventoryChannelEOF("command inventory idle channel closed")
            raise CustodyError("command inventory peer closed before a complete frame")
        newline = peek.find(b"\n")
        amount = len(peek) if newline < 0 else newline + 1
        part = connection.recv(amount)
        raw.extend(part)
        if len(raw) > FRAME_LIMIT:
            raise CustodyError("command inventory frame exceeds its hard limit")
        if raw.endswith(b"\n"):
            value = json.loads(bytes(raw[:-1]).decode("utf-8"))
            if not isinstance(value, dict):
                raise CustodyError("command inventory frame is not an object")
            return value


def _inventory_text(data: bytes) -> str:
    import io
    return io.TextIOWrapper(io.BytesIO(data), encoding=subprocess._text_encoding(), newline=None).read()


class _CommandInventoryOwner:
    """Original owner executes scoped sibling helpers and retains every handle."""

    def __init__(self, custody: Any, policy: Mapping[str, Any]):
        self.custody, self.policy = custody, dict(policy)
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.released = threading.Event()
        self.done = threading.Event()
        self.accepting = True
        self.pending: int | None = None
        self.last_operation = 0
        self.error: BaseException | None = None
        self.helpers: dict[int, Any] = {}
        self.actor = None
        self.thread = None
        self.channel_closed = False
        self.failure_stage = 'hello'
        self.failure_detail = None
        self.first_failure = None
        self.diagnostic_failure_type = None

    def _retain_first_failure(self, error: BaseException) -> None:
        # Optional redacted provenance never changes the initiating exception,
        # mandatory pending journal, or sticky closure verdict.
        if self.first_failure is not None:
            return
        try:
            chain = []
            seen = set()
            current = error
            while current is not None and id(current) not in seen and len(chain) < 8:
                seen.add(id(current))
                chain.append({'error_type': type(current).__name__[:128],
                              'message_sha256': _digest_bytes(str(current).encode())})
                current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
            record = {'event': 'command_inventory_first_failure',
                'owner_epoch': self.custody.owner_epoch,
                'run_id': self.custody._launch_broker.run_id,
                'root_pid': self.custody.process.pid,
                'operation_id': self.pending if self.pending is not None else (
                    self.last_operation if self.failure_stage == 'reply' else self.last_operation + 1),
                'substage': self.failure_stage, 'code': 'COMMAND_INVENTORY_FAILED',
                'detail': self.failure_detail,
                'chain': chain, 'authority': 'NONE', 'raw_message_emitted': False}
            self.first_failure = record
            _append_owner_jsonl(self.custody.root / 'custody.journal.jsonl', record)
        except BaseException as diagnostic_error:
            self.diagnostic_failure_type = type(diagnostic_error).__name__

    def _binding(self) -> dict[str, Any]:
        broker = self.custody._launch_broker
        reference = broker.role_authority.reference()
        return {'run_id': broker.run_id, 'owner_epoch': self.custody.owner_epoch,
                'generation': reference['generation'], 'target': reference['target']}

    def _authenticate(self, channel: socket.socket) -> dict[str, Any]:
        self.failure_stage = 'authenticate'
        broker = self.custody._launch_broker
        if self.actor is None:
            if channel is not broker.operation_channel:
                raise CustodyError('command inventory pipe is not the original host channel')
            actor = broker.retained_pipe_actor()
            identity = actor.verify()
            expected = broker.registration['identity']
            if self.custody.process is None or identity['pid'] != self.custody.process.pid or any(
                    identity.get(k) != expected.get(k) for k in ('pid', 'uid', 'birth_id')):
                raise CustodyError('command inventory peer is not the original root')
            broker.bind_ready_token(expected_pid=self.custody.process.pid,
                                    expected_identity=expected, token_provider=current_process_audit_token)
            self.actor = actor
        identity = self.actor.verify()
        target = self._binding()['target']
        if any(identity.get(k) != target.get(k) for k in (
                'pid', 'uid', 'birth_id', 'audit_token_sha256', 'audit_token_pidversion')):
            raise CustodyError('command inventory root custody changed')
        return self._binding()

    def _validate_policy(self, checkout: str, cwd: str, environment: Mapping[str, str], operation: str) -> None:
        import shutil
        p = self.policy
        self.failure_stage = 'policy_source_state'
        state = Path(p['state_path'])
        if state.is_symlink() or _digest_bytes(state.read_bytes()) != p['state_sha256']:
            raise CustodyError('command inventory declaration changed')
        self.failure_stage = 'policy_paths'
        pack_roots = {root for values in p['roots'].values() for root in values}
        allowed = set(p['roots']) if operation in {'revision', 'clean_status'} else pack_roots
        if checkout not in allowed:
            raise CustodyError('command inventory checkout is outside the launch binding')
        for value in {checkout, *p['roots'], *pack_roots}:
            path = Path(value)
            if not path.is_absolute() or path.is_symlink() or not path.is_dir() or str(path.resolve(strict=True)) != value:
                raise CustodyError('command inventory root identity changed')
        self.failure_stage = 'policy_cwd'
        allowed_cwd = {p['cwd'], *p['roots'], *pack_roots}
        if cwd not in allowed_cwd or str(Path(cwd).resolve(strict=True)) != cwd:
            raise CustodyError('command inventory caller cwd is unsupported')
        self.failure_stage = 'policy_environment'
        if (not isinstance(environment, dict) or len(environment) > 512
                or any(not isinstance(k, str) or not isinstance(v, str) or '\0' in k + v or '=' in k
                       for k, v in environment.items())):
            raise CustodyError('command inventory caller environment is malformed')
        keys = set(p['environment']) | set(environment)
        changed = [key for key in keys if not (
            key.startswith('ASTRID_RUNTIME_CUSTODY_') or key in {
                _COMMAND_INVENTORY_FD, 'ASTRID_COMMAND_CUSTODY_ENABLED'}
            # macOS may add this CoreFoundation encoding hint at exec. Only
            # an addition absent from the original launch is exempt: changes
            # or removal of a bound value and all other drift still fail.
            or (sys.platform == 'darwin' and key == '__CF_USER_TEXT_ENCODING'
                and key not in p['environment']))
            and environment.get(key) != p['environment'].get(key)]
        if changed:
            # Opaque KEY-name hashes only; no names or values are exported.
            self.failure_detail = {'field_name_sha256': sorted(_digest_bytes(key.encode()) for key in changed)[:16],
                                   'different_field_count': len(changed)}
            raise CustodyError('command inventory Git environment differs from its launch')
        self.failure_stage = 'policy_git_artifact'
        selected = shutil.which('git', path=environment.get('PATH'))
        if selected is None or str(Path(selected).resolve(strict=True)) != p['git'] or _digest_bytes(
                Path(p['git']).read_bytes()) != p['git_sha256']:
            raise CustodyError('command inventory Git artifact changed')

    def _execute(self, frame: Mapping[str, Any], channel: socket.socket) -> Any:
        import selectors
        from astrid.core.execution.process_group import popen_owned_process, terminate_tree, verify_tree_absent
        required = {'version', 'command', 'binding', 'operation_id', 'operation', 'checkout', 'cwd', 'environment'}
        with self.lock:
            self.failure_detail = None
            binding = self._authenticate(channel)
            self.failure_stage = 'request_binding'
            if (set(frame) != required or frame.get('version') != _COMMAND_INVENTORY_VERSION
                    or frame.get('command') != 'inventory_git' or frame.get('binding') != binding
                    or type(frame.get('operation_id')) is not int or frame['operation_id'] != self.last_operation + 1
                    or frame.get('operation') not in _COMMAND_GIT_OPERATIONS or not self.accepting
                    or self.pending is not None or self.error is not None):
                raise CustodyError('command inventory operation has an invalid binding')
            self._validate_policy(frame['checkout'], frame['cwd'], frame['environment'], frame['operation'])
            self.pending = self.last_operation = frame['operation_id']
            self.failure_stage = 'pending_journal'
            # Persist only identity/digests; never caller environment or credentials.
            _append_owner_jsonl(self.custody.root / 'custody.journal.jsonl', {
                'event': 'command_inventory_pending', 'operation_id': self.pending,
                'owner_epoch': self.custody.owner_epoch, 'root_generation': binding['generation'],
                'operation': frame['operation'], 'checkout_sha256': _digest_bytes(os.fsencode(frame['checkout']))})
        process = None
        try:
            argv = [self.policy['git'], '-C', frame['checkout'], *_COMMAND_GIT_OPERATIONS[frame['operation']]]
            env = dict(frame['environment'])
            env.pop(_COMMAND_INVENTORY_FD, None)
            self.failure_stage = 'helper_launch'
            process = popen_owned_process(argv, cwd=frame['cwd'], env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          _delegate_inventory=False)
            with self.lock:
                self.helpers[self.pending] = process
            self.failure_stage = 'helper_stream'
            selector = selectors.DefaultSelector()
            output = {'stdout': bytearray(), 'stderr': bytearray()}
            for name in output:
                selector.register(getattr(process, name), selectors.EVENT_READ, name)
            deadline = time.monotonic() + 120
            try:
                while selector.get_map() or process.poll() is None:
                    if self.cancelled.is_set() or time.monotonic() >= deadline:
                        raise CustodyError('command inventory operation cancelled or timed out')
                    self._authenticate(channel)
                    self.failure_stage = 'helper_stream'
                    try:
                        queued = channel.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                        if queued == b'':
                            raise CustodyError('command inventory caller disconnected')
                        if queued:
                            raise CustodyError('command inventory concurrent operation is forbidden')
                    except BlockingIOError:
                        pass
                    for key, _ in selector.select(.05):
                        value = os.read(key.fd, 8192)
                        if not value:
                            selector.unregister(key.fileobj)
                        else:
                            output[key.data].extend(value)
                            if len(output[key.data]) > (16 * 1024 * 1024 if key.data == 'stdout' else 1024 * 1024):
                                raise CustodyError('command inventory output exceeded its bound')
                self.failure_stage = 'helper_original_wait'
                code = process.wait(timeout=0)
            finally:
                selector.close()
            observer = getattr(process._astrid_custody, '_observer', None)
            if observer is not None:
                observer.join(2)
            self.failure_stage = 'helper_closure'
            verify_tree_absent(process)
            if type(code) is not int:
                raise CustodyError('command inventory original wait is unavailable')
            with self.lock:
                if self._authenticate(channel) != binding:
                    raise CustodyError('command inventory root changed during execution')
                self.failure_stage = 'completion_journal'
                _append_owner_jsonl(self.custody.root / 'custody.journal.jsonl', {
                    'event': 'command_inventory_completed', 'operation_id': frame['operation_id'],
                    'helper_pid': process.pid, 'original_wait': code, 'helper_graph': 'VERIFIED'})
                self.pending = None
            return subprocess.CompletedProcess(argv, code, bytes(output['stdout']), bytes(output['stderr']))
        except BaseException as error:
            self._retain_first_failure(error)
            if process is None:
                process = getattr(error, 'process', None)
                if process is not None:
                    self.helpers[frame['operation_id']] = process
            if process is not None:
                try:
                    if process.poll() is None:
                        terminate_tree(process)
                    process.wait(timeout=5)
                    verify_tree_absent(process)
                except BaseException as cleanup_error:
                    self.error = cleanup_error
            self.error = self.error or error
            raise CustodyError('command inventory ownership remains uncertain') from error
        finally:
            if process is not None:
                for name in ('stdout', 'stderr'):
                    stream = getattr(process, name, None)
                    if stream is not None:
                        stream.close()

    def abort_before_release(self) -> None:
        with self.lock:
            self.accepting = False
            self.cancelled.set()
            self.error = self.error or CustodyError('command inventory original launch failed')
            self.released.set()
        channel = self.custody._launch_broker.operation_channel
        if channel is not None:
            try:
                channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def start(self) -> None:
        def serve():
            channel = self.custody._launch_broker.operation_channel
            try:
                self.released.wait()
                if self.cancelled.is_set():
                    raise CustodyError('command inventory original launch failed')
                if channel is None:
                    raise CustodyError('command inventory channel is unavailable')
                channel.settimeout(None)
                self.failure_stage = 'hello'
                hello = _read_inventory_frame(channel)
                if hello != {'version': _COMMAND_INVENTORY_VERSION, 'command': 'inventory_hello'}:
                    raise CustodyError('command inventory greeting is invalid')
                _send_frame(channel, {'version': _COMMAND_INVENTORY_VERSION, 'status': 'ready',
                                      'binding': self._authenticate(channel)})
                while True:
                    self.failure_stage = 'request_frame'
                    frame = _read_inventory_frame(channel)
                    result = self._execute(frame, channel)
                    base = {'version': _COMMAND_INVENTORY_VERSION, 'operation_id': frame['operation_id'],
                            'binding': frame['binding']}
                    self.failure_stage = 'reply'
                    for name in ('stdout', 'stderr'):
                        data = getattr(result, name)
                        for offset in range(0, len(data), 8192):
                            _send_frame(channel, {**base, 'status': 'chunk', 'stream': name,
                                'data': base64.b64encode(data[offset:offset + 8192]).decode('ascii')})
                    _send_frame(channel, {**base, 'status': 'completed', 'returncode': result.returncode})
            except _InventoryChannelEOF as error:
                with self.lock:
                    self.channel_closed = True
                    if self.pending is not None:
                        self._retain_first_failure(error)
                        self.error = self.error or error
                    self.accepting = False
            except BaseException as error:
                self._retain_first_failure(error)
                # Non-EOF protocol/auth/output failures remain sticky even if
                # the original root exits while the exception is handled.
                with self.lock:
                    self.error = self.error or error
                    self.accepting = False
            finally:
                if channel is not None:
                    channel.close()
                self.done.set()
        self.thread = threading.Thread(target=serve, name='astrid-command-inventory', daemon=True)
        self.thread.start()

    def finish(self, *, cancel: bool = False) -> None:
        with self.lock:
            self.accepting = False
            if cancel:
                self.cancelled.set()
        if not self.done.wait(120):
            raise CustodyError('command inventory original owner is unresolved')
        if self.error is not None or self.pending is not None:
            raise CustodyError('command inventory delegated closure is uncertain') from self.error


class _CommandInventoryClient:
    def __init__(self, descriptor: str):
        self.creator_pid = os.getpid()
        self.channel = _PipeChannel.from_binding(descriptor)
        self.channel.set_inheritable(False)
        self.channel.settimeout(120)
        self.lock = threading.Lock()
        self.operation = 0
        self.error: BaseException | None = None
        try:
            _send_frame(self.channel, {'version': _COMMAND_INVENTORY_VERSION, 'command': 'inventory_hello'})
            reply = _read_inventory_frame(self.channel)
            if reply.get('version') != _COMMAND_INVENTORY_VERSION or reply.get('status') != 'ready' or not isinstance(reply.get('binding'), dict):
                raise CustodyError('command inventory owner greeting is invalid')
            self.binding = reply['binding']
            if self.binding.get('target', {}).get('pid') != self.creator_pid:
                raise CustodyError('command inventory client is not the authenticated root')
        except BaseException:
            self.channel.close()
            raise

    def run(self, operation: str, checkout: str, cwd: str, environment: dict[str, str]) -> Any:
        with self.lock:
            if self.creator_pid != os.getpid():
                raise CustodyError('command inventory client cannot be inherited')
            if self.error is not None:
                raise CustodyError('command inventory client remains uncertain') from self.error
            try:
                self.operation += 1
                base = {'version': _COMMAND_INVENTORY_VERSION, 'binding': self.binding,
                        'operation_id': self.operation}
                _send_frame(self.channel, {**base, 'command': 'inventory_git', 'operation': operation,
                    'checkout': checkout, 'cwd': cwd, 'environment': environment})
                output = {'stdout': bytearray(), 'stderr': bytearray()}
                while True:
                    reply = _read_inventory_frame(self.channel)
                    if any(reply.get(k) != v for k, v in base.items()):
                        raise CustodyError('command inventory reply has a stale binding')
                    if reply.get('status') == 'completed':
                        if set(reply) != {*base, 'status', 'returncode'}:
                            raise CustodyError('command inventory completion frame is malformed')
                        code = reply.get('returncode')
                        if type(code) is not int:
                            raise CustodyError('command inventory original wait is unavailable')
                        return subprocess.CompletedProcess(['git', '-C', checkout, *_COMMAND_GIT_OPERATIONS[operation]], code,
                                                           _inventory_text(bytes(output['stdout'])), _inventory_text(bytes(output['stderr'])))
                    if (set(reply) != {*base, 'status', 'stream', 'data'} or reply.get('status') != 'chunk'
                            or reply.get('stream') not in output or not isinstance(reply.get('data'), str)):
                        raise CustodyError('command inventory output frame is malformed')
                    data = base64.b64decode(reply.get('data', ''), validate=True)
                    name = reply['stream']
                    if len(data) > 8192 or len(output[name]) + len(data) > (16 * 1024 * 1024 if name == 'stdout' else 1024 * 1024):
                        raise CustodyError('command inventory output exceeded its bound')
                    output[name].extend(data)
            except BaseException as error:
                self.error = error
                self.channel.close()
                raise CustodyError('command inventory transport or custody failed') from error


def command_inventory_git(args: Sequence[str], *, cwd: Path | None = None) -> Any | None:
    """No-channel/provisioning behavior stays unchanged; no failure fallback."""
    if sys.platform != 'darwin' or _COMMAND_INVENTORY_FD not in os.environ:
        return None
    if len(args) < 3 or args[0] != '-C':
        return None
    operation = next((name for name, tail in _COMMAND_GIT_OPERATIONS.items() if tuple(args[2:]) == tail), None)
    if operation is None:
        return None
    try:
        descriptor = os.environ[_COMMAND_INVENTORY_FD]
        _validate_pipe_binding(json.loads(descriptor))
        key = (os.getpid(), descriptor)
        with _COMMAND_CLIENTS_LOCK:
            client = _COMMAND_CLIENTS.get(key)
            if client is None:
                client = _COMMAND_CLIENTS[key] = _CommandInventoryClient(descriptor)
        return client.run(operation, str(Path(args[1]).resolve(strict=True)),
                          str(Path(cwd or os.getcwd()).resolve(strict=True)), dict(os.environ))
    except CustodyError:
        raise
    except BaseException as error:
        raise CustodyError('command inventory context is unavailable') from error


_FAILED_LAUNCH_REAP_TIMEOUT_SECONDS = 5.0


class CustodyRootBinding:
    """Host-created storage bound to one live lifecycle/owner incarnation.

    This is an in-memory launch input, never reconstructed from executor JSON,
    environment or an on-disk record. Missing roots are not recreated.
    """

    @staticmethod
    def _identity(path):
        path = Path(path)
        try:
            if not path.is_absolute() or path.resolve(strict=True) != path:
                raise CustodyError("custody root must be canonical and unsymlinked")
            value = path.lstat()
        except OSError as error:
            raise CustodyError("custody root is missing") from error
        if (not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid()
                or stat.S_IMODE(value.st_mode) != 0o700):
            raise CustodyError("custody root must be an owner-only directory")
        return value.st_dev, value.st_ino, value.st_uid

    def __init__(self, lifecycle_root: Path, *, lifecycle_identity: tuple[int, int, int],
                 owner_epoch: str):
        from .process_group import process_birth_identity
        self.lifecycle_root = Path(lifecycle_root)
        self.lifecycle_identity = lifecycle_identity
        if self._identity(self.lifecycle_root) != lifecycle_identity:
            raise CustodyError("custody lifecycle root was substituted")
        if not isinstance(owner_epoch, str) or re.fullmatch(r"[0-9a-f]{64}", owner_epoch) is None:
            raise CustodyError("custody owner epoch is invalid")
        self.owner_epoch = owner_epoch
        self.owner_pid, self.owner_birth = os.getpid(), process_birth_identity()
        if not self.owner_birth:
            raise CustodyError("custody storage owner incarnation is unavailable")
        self.root = self.lifecycle_root / ("custody-" + owner_epoch)
        self.root.mkdir(mode=0o700)  # Never reuse another session's directory.
        self.root_identity = self._identity(self.root)
        self.verify()
        _atomic_owner_json(self.root / "custody-owner.json", {
            "owner_epoch": owner_epoch, "owner_pid": self.owner_pid,
            "owner_birth": self.owner_birth, "lifecycle_identity": lifecycle_identity,
            "custody_identity": self.root_identity,
        })

    def verify(self):
        from .process_group import process_birth_identity
        if (os.getpid() != self.owner_pid or process_birth_identity() != self.owner_birth
                or self._identity(self.lifecycle_root) != self.lifecycle_identity
                or self._identity(self.root) != self.root_identity):
            raise CustodyError("custody root or owner incarnation was substituted")


class ProcessCustody:
    """Retain every registered incarnation independently of ancestry/session.

    Darwin roles consume the Runtime designation ABI. Linux local children use
    retained pidfds and an immutable owner; no transferable Runtime role reference
    is fabricated for that backend. Failed registration keeps the obligation.
    """

    def __init__(self, *, scope_root: Path | None = None, owner_epoch: str | None = None,
                 command_diagnostics: bool | None = None,
                 custody_root: CustodyRootBinding | None = None):
        if custody_root is not None:
            if type(custody_root) is not CustodyRootBinding or scope_root is not None or owner_epoch is not None:
                raise CustodyError("confined custody requires one explicit host root binding")
            custody_root.verify()
        self._custody_root = custody_root
        self.root = custody_root.root if custody_root is not None else (scope_root or Path(tempfile.mkdtemp(prefix="astrid-custody-")))
        self.owner_epoch = custody_root.owner_epoch if custody_root is not None else (owner_epoch or _digest_bytes(os.urandom(32)))
        self._lock = threading.RLock()
        self.command_diagnostics = (os.environ.get("ASTRID_CUSTODY_COMMAND_DIAGNOSTICS") == "1"
                                    if command_diagnostics is None else command_diagnostics)
        self._command_observations: list[dict[str, Any]] = []
        self._actor_birth: str | None = None
        self._handles: dict[int, tuple[str, Any]] = {}
        self._unadmitted_absent: dict[int, dict[str, Any]] = {}
        self._observed_candidates: dict[int, dict[str, Any]] = {}
        self._census_sequence = 0
        self._uncertain: BaseException | None = None
        self._launch_broker: RoleBoundCustodyBroker | None = None
        self._inventory_owner: _CommandInventoryOwner | None = None
        self.process: Any = None
        if sys.platform == "darwin":
            self.actor = AuthenticatedCleanupActor.current()
        else:
            self.actor = None

    def _verify_root(self):
        binding = getattr(self, "_custody_root", None)
        if binding is not None:
            binding.verify()

    def prepare_launch(self, argv: Sequence[str], *, start_new_session: bool, role: str = "command", inventory_context: Mapping[str, Any] | None = None, child_bridge_launch=None) -> tuple[list[str], dict[str, str]]:
        self._verify_root()
        if sys.platform != "darwin":
            if child_bridge_launch is not None:
                raise CustodyError('matched child bridge bootstrap requires Darwin custody')
            if not (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")):
                raise CustodyError("incarnation-bound kernel custody is unavailable")
            return list(argv), {}
        policy = _inventory_launch_policy(inventory_context["environment"], inventory_context["cwd"]) if inventory_context is not None and role == "command" else None
        broker = RoleBoundCustodyBroker(
            role=role, retain_channel=policy is not None, identity_provider=default_process_identity,
            ledger_root=self.root / (role + "-ledger-" + os.urandom(8).hex()),
            authority_scope_root=self.root,
            authority_journal=self.root / "custody.journal.jsonl",
            owner_epoch=self.owner_epoch,
        )
        self._launch_broker = broker
        try:
            if child_bridge_launch is not None:
                if policy is not None or role != 'command':
                    raise CustodyError('child bridge cannot inherit custody control endpoints')
                broker.child_bridge_binding = child_bridge_launch.prepare(self, argv)
            environment = broker.child_environment(argv, start_new_session=start_new_session)
        except BaseException:
            broker.abort_before_spawn()
            raise
        if policy is not None:
            self._inventory_owner = _CommandInventoryOwner(self, policy)
            environment["ASTRID_COMMAND_CUSTODY_ENABLED"] = "1"
        return custody_wrapper_argv(sys.executable), environment

    def bind_launch(self, process: Any) -> None:
        self.process = process  # Retain before any seal or observation can fail.
        try:
            self._verify_root()
            if self._launch_broker is not None:
                self._launch_broker.close_child_pipes()
                self._launch_broker._launch_pid = process.pid  # Diagnostic selector only.
                self._launch_broker.wait_until_sealed()
                registration = self._launch_broker.registration
                if registration is None or registration["pid"] != process.pid:
                    raise CustodyError("registered launch differs from retained child")
                self._launch_broker.retain_launch_child(process)
                birth = str(registration["identity"]["birth_id"])
                self._handles[process.pid] = (birth, self._launch_broker)
            else:
                from astrid.core.execution.process_group import process_birth_identity
                self._actor_birth = process_birth_identity()
                self.register(process.pid, process_birth_identity(process.pid))
        except BaseException as exc:
            self._uncertain = exc
            process._astrid_tree_uncertain = str(exc) or type(exc).__name__
            raise

    def begin_launch_spawn(self) -> None:
        if self._launch_broker is not None:
            self._launch_broker.role_authority.begin_spawn()

    def settle_launch(self) -> None:
        self._verify_root()
        if self._launch_broker is not None:
            if self.process is None:
                raise CustodyError('settlement has no original retained child')
            self._launch_broker.settle_launch()

    def start_inventory_owner(self) -> None:
        if getattr(self, "_inventory_owner", None) is not None:
            self._inventory_owner.start()

    def cancel_inventory_owner(self) -> None:
        if getattr(self, "_inventory_owner", None) is not None:
            with self._inventory_owner.lock:
                self._inventory_owner.accepting = False
                self._inventory_owner.cancelled.set()

    def finish_inventory_owner(self, *, cancel: bool = False) -> None:
        if getattr(self, "_inventory_owner", None) is not None:
            self._inventory_owner.finish(cancel=cancel)

    def register(self, pid: int, birth: str, *, _observation: Mapping[str, Any] | None = None,
                 _capture_retry: bool = False) -> None:
        with self._lock:
            stage = "birth-check"
            try:
                self._verify_root()
                if not birth:
                    raise CustodyError("process registration lacks a birth identity")
                if pid in getattr(self, "_unadmitted_absent", {}):
                    self._uncertain = CustodyError("unadmitted absent candidate cannot gain registration authority")
                    raise self._uncertain
                if pid in self._handles:
                    if self._handles[pid][0] != birth:
                        raise CustodyError("registered process identity was reused")
                    return
                if sys.platform == "darwin":
                    stage = "initial-identity"
                    if _observation is None:
                        identity = default_process_identity(pid)
                    else:
                        outcome = _observation["outcome"]
                        if outcome == "OBSERVATION_ERROR":
                            raise _observation["error"]
                        identity = _observation.get("identity")
                        if outcome == "ZOMBIE":
                            raise UnadmittedCandidateZombie(_observation)
                        if outcome not in {"LIVE", "ABSENT"}:
                            raise CustodyError("ordinary candidate outcome is invalid")
                    if identity is None:
                        raise UnadmittedCandidateAbsent("descendant registration identity is uncertain (identity_outcome=absent)")
                    if identity["birth_id"] != birth:
                        observed_birth = identity.get("birth_id")
                        rendered_birth = json.dumps(observed_birth[:160]) if isinstance(observed_birth, str) else "invalid"
                        raise CustodyError(
                            "descendant registration identity is uncertain "
                            f"(identity_outcome=birth-mismatch, observed_birth={rendered_birth})"
                        )
                    if getattr(self, "command_diagnostics", False):
                        # Explicit diagnostic owners only; no signal authority
                        # or acceptance depends on this optional metadata.
                        entries = self._command_observations
                        if len(entries) < 64:
                            try:
                                target = _command_observation(pid)
                                parent = identity.get("parent_pid")
                                entries.append({"candidate_pid": pid, "observer_pid": os.getpid(),
                                                "stage": "before_initial_token_capture", "target": target,
                                                "parent": _command_observation(parent) if type(parent) is int and parent > 0 else None})
                            except BaseException as diagnostic_error:
                                entries.append({"candidate_pid": pid, "observer_pid": os.getpid(),
                                                "stage": "before_initial_token_capture", "authority": "NONE",
                                                "outcome": "OBSERVATION_ERROR", "error_type": type(diagnostic_error).__name__})
                        else:
                            self._command_diagnostics_truncated = True
                    role = "descendant_" + str(pid) + "_" + hashlib.sha256(birth.encode()).hexdigest()[:12]
                    initial_capture_witness = None
                    broker = getattr(self, "_launch_broker", None)
                    original_native = _observation.get("native") if isinstance(_observation, Mapping) else None
                    # A bounded retry admits only the exact token it captures.
                    # Its earlier failed capture cannot be replaced by a new
                    # original-capture witness granting exec-refresh authority.
                    if (not _capture_retry
                            and isinstance(broker, RoleBoundCustodyBroker) and broker.role == "command"
                            and isinstance(original_native, Mapping)):
                        stage = "initial-start-witness"
                        try:
                            started = _darwin_process_start(pid)
                        except DarwinProcessExitedError:
                            started = None  # No anchor; existing capture/terminal reconciliation remains authoritative.
                        if started is not None:
                            fields = ("pid", "uid", "start_sec", "start_usec")
                            if any(type(original_native.get(k)) is not int or started.get(k) != original_native[k] for k in fields):
                                raise CustodyError("initial start differs from original precise native census")
                            anchor = _start_anchor(started)
                            deadline = time.monotonic() + 0.1
                            fence = _darwin_absolute_time()
                            while fence <= anchor["start_abstime"]:
                                if fence < anchor["start_abstime"] or time.monotonic() >= deadline:
                                    raise CustodyError("initial monotonic birth fence failed")
                                time.sleep(0.001)
                                fence = _darwin_absolute_time()
                            initial_capture_witness = {
                                "anchor": anchor, "clock_fence": fence, "actor": self.actor.verify(),
                                "owner_pid": os.getpid(), "role": role, "generation": 1,
                            }
                    stage = "token-capture"
                    try:
                        token = current_process_audit_token(pid)
                    except NativeTokenCaptureError as capture_error:
                        # Observe immediately, before diagnostic evidence I/O.
                        post = ordinary_candidate_observation(pid)
                        capture_error.candidate_observations = {
                            "pre": _candidate_observation_evidence(_observation),
                            "post": _candidate_observation_evidence(post),
                            "ordering": ["pre_capture", "capture_failure", "post_failure_native_observation", "evidence_persist"],
                        }
                        if getattr(self, "command_diagnostics", False):
                            # Failure-specific bounded rows bypass the first-64
                            # history limit. Optional I/O starts only AFTER the
                            # immediate native post-failure observation above.
                            parent = identity.get("parent_pid")
                            try:
                                capture_error.failure_command_observations = {
                                    "candidate_pid": pid, "parent_pid": parent,
                                    "target": _command_observation(pid),
                                    "parent": _command_observation(parent)
                                    if type(parent) is int and parent > 0 else None,
                                    "authority": "NONE",
                                }
                            except BaseException as diagnostic_error:
                                capture_error.failure_command_observations = {
                                    "candidate_pid": pid, "parent_pid": parent,
                                    "classification": "UNPROVED", "authority": "NONE",
                                    "error_type": type(diagnostic_error).__name__,
                                }
                        raise
                    # Recheck the observation after binding the exact kernel token.
                    stage = "post-token-identity"
                    post = ordinary_candidate_observation(pid)
                    if post["outcome"] in {"ABSENT", "ZOMBIE"}:
                        raise _PostTokenCandidateTerminal(_observation, post, token)
                    if post["outcome"] == "OBSERVATION_ERROR":
                        raise post["error"]
                    if post["outcome"] != "LIVE" or post.get("identity") != identity:
                        raise CustodyError("descendant changed during registration")
                    if isinstance(_observation, Mapping) and isinstance(_observation.get('native'), Mapping):
                        before_native = _observation['native']
                        after_native = post.get('native')
                        precise = ('pid', 'uid', 'parent_pid', 'pgid', 'start_sec', 'start_usec')
                        if (not isinstance(after_native, Mapping)
                                or any(type(before_native.get(k)) is not int
                                       or type(after_native.get(k)) is not int
                                       or before_native[k] != after_native[k] for k in precise)):
                            raise CustodyError('descendant precise native incarnation changed during registration')
                    stage = "designation-bind"
                    witness = initial_capture_witness
                    if witness is not None:
                        try:
                            post_capture_start = _darwin_process_start(pid)
                        except DarwinProcessExitedError:
                            witness = None
                    if witness is not None:
                        if (witness["owner_pid"] != os.getpid() or witness["actor"] != self.actor.verify()
                                or _darwin_absolute_time() < witness["clock_fence"]
                                or _start_anchor(post_capture_start) != witness["anchor"]):
                            raise CustodyError("original capture differs from initial start witness")
                        _validated_token(token, witness["anchor"])
                    authority = RoleCustodyAuthority(self.root, role)
                    authority._initial_capture_witness = witness
                    authority.designate_pending(actor=self.actor, identity=identity, token=token, owner_epoch=self.owner_epoch)
                    reference = authority.bind_target(actor=self.actor, generation=1, identity=identity, token=token)
                    # Retain the actual admitted capability before any optional
                    # prospective enrollment. Old/disk handles are never upgraded.
                    self._handles[pid] = (birth, (authority, reference))
                    if witness is not None:
                        stage = "descendant-start-anchor"
                        try:
                            authority.establish_start_anchor(
                                actor=self.actor, generation=reference["generation"],
                                expected_target=reference["target"], authenticated_token=token,
                                require_suspended=False,
                            )
                        except _StartAnchorTargetGone:
                            pass  # Original fixed-token obligation retained; no cleanup inference.
                        else:
                            self._handles[pid] = (birth, (authority, authority.reference()))
                    if getattr(self, "command_diagnostics", False):
                        try:
                            self._write_observed_containment()
                        except Exception as diagnostic_error:
                            # Optional evidence I/O cannot alter acquired custody.
                            self._command_diagnostic_persist_error = type(diagnostic_error).__name__
                else:
                    from astrid.core.execution.process_group import process_birth_identity
                    if process_birth_identity(pid) != birth:
                        raise CustodyError("descendant registration identity is uncertain")
                    stage = "pidfd-bind"
                    descriptor = os.pidfd_open(pid, 0)
                    if process_birth_identity(pid) != birth:
                        os.close(descriptor)
                        raise CustodyError("descendant changed during pidfd binding")
                    os.set_inheritable(descriptor, False)
                    self._handles[pid] = (birth, descriptor)
            except Exception as exc:
                if sys.platform != "darwin":
                    raise
                error = CustodyError(
                    f"descendant registration failed (stage={stage}, candidate_pid={pid}, "
                    f"expected_birth={birth}, observer_pid={os.getpid()}, "
                    f"root_pid={getattr(getattr(self, 'process', None), 'pid', None)}): {exc}"
                )
                error.registration_stage = stage
                raise error from exc

    def _observe_candidate_exits(self, observations):
        watches = self._exit_observations = getattr(self, "_exit_observations", {})
        for pid, observation in observations.items():
            if observation.get("outcome") != "LIVE" or not isinstance(observation.get("native"), Mapping):
                continue
            native = observation["native"]
            key = (pid, native.get("start_sec"), native.get("start_usec"))
            if key not in watches:
                watches[key] = _DarwinExitObservation(observation)
        if not watches:
            return
        self._write_observed_containment()
        thread = getattr(self, "_exit_observer_thread", None)
        if thread is not None and thread.is_alive():
            return

        def monitor():
            while True:
                with self._lock:
                    active = [watch for watch in watches.values() if watch.queue is not None]
                    if not active:
                        return
                    changed = any([watch.poll() for watch in active])
                    if changed:
                        try:
                            self._write_observed_containment()
                        except (OSError, CustodyError):
                            # Preserve in memory; observation cannot release a
                            # custody fence or mutate existing capabilities.
                            self._exit_evidence_persist_failed = True
                time.sleep(0.01)
        self._exit_observer_thread = threading.Thread(target=monitor, name="astrid-exit-observer", daemon=True)
        self._exit_observer_thread.start()

    def _write_observed_containment(self) -> None:
        self._verify_root()
        # This is retained evidence, not a designation or signal capability.
        _atomic_owner_json(self.root / "observed-containment.json", {
            "version": "astrid.observed-process-containment/v1",
            "coverage": "retained_observed_set_and_final_owned_group_checks",
            "complete_descendant_history": "UNPROVED",
            "owner_epoch": self.owner_epoch,
            "unadmitted_candidates": list(self._unadmitted_absent.values()),
            "observed_candidates": list(getattr(self, "_observed_candidates", {}).values()),
            "capture_failures": getattr(self, "_capture_failure_observations", []),
            "exit_observations": [watch.facts for watch in getattr(self, "_exit_observations", {}).values()],
            **({"command_observations": self._command_observations,
                "command_diagnostics_truncated": getattr(self, "_command_diagnostics_truncated", False),
                "command_diagnostic_authority": "NONE",
                "command_diagnostic_persist_error": getattr(self, "_command_diagnostic_persist_error", None)}
               if getattr(self, "command_diagnostics", False) else {}),
        })

    def _collect_census_candidates(self, members, census):
        # Membership is evidence only. Include children even when the caller's
        # traversal stopped at a vanished intermediary; never mint handles here.
        selected = set(members) | set(self._handles) | set(getattr(self, "_unadmitted_absent", {}))
        selected.update(getattr(self, "_observed_candidates", {}))
        while True:
            children = {pid for pid, row in census.items() if row["parent_pid"] in selected}
            if children <= selected:
                break
            selected.update(children)
        candidates = dict(members)
        records = self._observed_candidates = getattr(self, "_observed_candidates", {})
        changed = False
        for pid, row in census.items():
            if pid not in selected:
                continue
            candidates.setdefault(pid, row["birth_id"])
            if pid in self._handles:
                continue
            previous = records.get(pid)
            if previous is not None:
                # Preserve the first precise edge even when a later observation
                # has the same child identity but has lost its parent row.
                if previous["identity"] != dict(row) and previous.get("latest_census") != dict(row):
                    previous["latest_census"] = dict(row)
                    changed = True
            else:
                records[pid] = {"pid": pid, "identity": dict(row),
                    "parent_edge": {"child_pid": pid, "parent_pid": row["parent_pid"],
                                    "parent_identity": dict(census[row["parent_pid"]])
                                    if row["parent_pid"] in census else None},
                    "observation_sequence": getattr(self, "_census_sequence", 0),
                    "admitted": False, "capability": "NONE"}
                changed = True
        if changed:
            self._write_observed_containment()
        return candidates

    def _validate_candidate_chain(self, pid, birth, census):
        # An old census edge or terminal tombstone is never a live capability.
        row = census.get(pid)
        if row is None or row.get("birth_id") != birth:
            raise CustodyError("live candidate lacks matching census evidence")
        seen = {pid}
        parent = row["parent_pid"]
        while True:
            ancestor = census.get(parent)
            if parent in seen or ancestor is None or parent in self._unadmitted_absent:
                raise CustodyError("census child requires unadmitted absent ancestry")
            seen.add(parent)
            if ancestor.get("status") == 5:
                raise CustodyError("live candidate requires zombie admitted-parent ancestry")
            if parent in self._handles:
                if self._handles[parent][0] != ancestor.get("birth_id"):
                    raise CustodyError("live candidate ancestry incarnation changed")
                return
            observed = ordinary_candidate_observation(parent)
            native = observed.get("native")
            fields = ("pid", "uid", "parent_pid", "pgid", "start_sec", "start_usec")
            if (observed.get("outcome") != "LIVE" or not isinstance(native, Mapping)
                    or observed.get("identity", {}).get("birth_id") != ancestor.get("birth_id")
                    or any(type(native.get(k)) is not int or native[k] != ancestor.get(k) for k in fields)):
                raise CustodyError("live candidate ancestry is no longer verifiable")
            parent = ancestor["parent_pid"]

    def _check_unadmitted_census(self, census: Mapping[int, Mapping[str, Any]]) -> None:
        if not census:
            raise CustodyError("unadmitted candidate reconciliation census unavailable")
        changed = False
        for pid, record in self._unadmitted_absent.items():
            row = census.get(pid)
            if record.get("outcome") == "ZOMBIE":
                if row is not None:
                    if record.get("absence_observed"):
                        raise CustodyError("terminal candidate reappeared after absence")
                    self._validate_terminal_observation(pid, record["birth_id"], row,
                                                        record["terminal_observation"])
                elif not record.get("absence_observed"):
                    record["absence_observed"] = True
                    changed = True
            elif row is not None:
                raise CustodyError("unadmitted absent candidate reappeared or was reused")
            if any(row["parent_pid"] == pid and child not in self._handles
                   for child, row in census.items()):
                raise CustodyError("unverified child of unadmitted absent candidate")

        if changed:
            self._write_observed_containment()

        for pid, (birth, _) in self._handles.items():
            if pid in census and census[pid]["birth_id"] != birth:
                raise CustodyError("admitted process changed during candidate reconciliation")
            if pid not in census and any(row["parent_pid"] == pid and child not in self._handles
                                        for child, row in census.items()):
                raise CustodyError("unverified child of missing admitted parent")
            if (pid in census and census[pid].get("status") == 5
                    and any(row["parent_pid"] == pid and child not in self._handles
                            for child, row in census.items())):
                raise CustodyError("unverified child requires zombie admitted-parent ancestry")

    def _owned_census_members(self, census: Mapping[int, Mapping[str, Any]]) -> dict[int, str]:
        # Follow only live retained incarnations, then the children actually
        # observed beneath them. Never follow an absent/recycled parent PID.
        owned = {pid: birth for pid, (birth, _) in self._handles.items()
                 if pid in census and census[pid]["birth_id"] == birth}
        pending = list(owned)
        while pending:
            parent = pending.pop()
            for pid, row in census.items():
                if row["parent_pid"] == parent and pid not in owned and pid not in self._unadmitted_absent:
                    owned[pid] = row["birth_id"]
                    pending.append(pid)
        return owned

    def validate_final_census(self, census: Mapping[int, Mapping[str, Any]]) -> None:
        """No final observation may bypass retained identities or tombstones."""
        with self._lock:
            if self._uncertain is not None:
                raise CustodyError("custody registration or observation is unresolved") from self._uncertain
            if sys.platform != "darwin" or getattr(getattr(self, "_launch_broker", None), "role", None) != "command":
                return
            try:
                self._verify_root()
                self.actor.verify()
                self._collect_census_candidates({}, census)
                self._check_unadmitted_census(census)
                if any(pid in census for pid in self._unadmitted_absent):
                    raise CustodyError("observed terminal candidate remains in final census")
                if self._owned_census_members(census):
                    raise CustodyError("retained observed processes remain in final census")
            except BaseException as error:
                self._uncertain = error
                raise

    def observe_census(self, members: Mapping[int, str], *,
                       census: Mapping[int, Mapping[str, Any]],
                       fresh_census: Callable[[], Mapping[int, Mapping[str, Any]]]) -> dict[int, str]:
        """Reconcile new ordinary Darwin candidates without claiming their exit.

        Direct register/observe remain strict. Only a command's retained owner
        can record this pre-admission disposition. Admitted handles and all
        other registration failures retain their existing semantics.
        """
        broker = getattr(self, "_launch_broker", None)
        if (sys.platform != "darwin" or not isinstance(broker, RoleBoundCustodyBroker)
                or broker.role != "command"):
            self.observe(members)
            return dict(members)
        with self._lock:
            if self._uncertain is not None:
                raise CustodyError("custody registration or observation is unresolved") from self._uncertain
            try:
                self._verify_root()
                self._unadmitted_absent = getattr(self, "_unadmitted_absent", {})
                self._census_sequence = getattr(self, "_census_sequence", 0) + 1
                root_pid = self.process.pid
                root_handle = self._handles.get(root_pid)
                if root_handle is None or root_handle[1] is not broker:
                    raise CustodyError("ordinary census lacks its retained admitted root")
                actor = self.actor.verify()
                members = self._collect_census_candidates(members, census)
                self._check_unadmitted_census(census)
                previously_admitted = set(self._handles)
                # A reconciliation census is an observation too. Account for
                # its fresh children before accepting a disposition, boundedly.
                for reconciliation_round in range(8):
                    new_absences = []
                    retry_admissions = []
                    # Observe the whole set before any terminal disposition.
                    observations = {}
                    for pid in members:
                        if pid not in self._handles and pid not in self._unadmitted_absent:
                            observations[pid] = ordinary_candidate_observation(pid)
                            self._observe_candidate_exits({pid: observations[pid]})
                    # Live acquisition precedes terminal reconciliation, regardless
                    # of mapping order. Each live child still needs its own chain.
                    ordered = sorted(members, key=lambda pid: observations.get(pid, {}).get("outcome") != "LIVE")
                    for pid in ordered:
                        birth = members[pid]
                        if pid in self._unadmitted_absent:
                            if birth != self._unadmitted_absent[pid]["birth_id"]:
                                raise CustodyError("terminal candidate member identity changed")
                            continue  # Observation only: never upgrade a terminal candidate.
                        self._register_census_candidate(pid, birth, census=census,
                                                        root_pid=root_pid, root_handle=root_handle,
                                                        actor=actor, previously_admitted=previously_admitted,
                                                        census_round=reconciliation_round,
                                                        new_absences=new_absences,
                                                        fresh_census=fresh_census,
                                                        retry_admissions=retry_admissions,
                                                        _collected_observation=observations.get(pid))
                    if not new_absences and not retry_admissions:
                        return {pid: birth for pid, birth in members.items()
                                if pid not in self._unadmitted_absent}
                    fresh = fresh_census()
                    self._collect_census_candidates({}, fresh)
                    self._check_unadmitted_census(fresh)
                    self._revalidate_post_absence(fresh, new_absences, actor, root_pid, root_handle)
                    fresh_members = self._owned_census_members(fresh)
                    for pid in new_absences:
                        record = self._unadmitted_absent[pid]
                        record["disposition"] = ("UNADMITTED_ZOMBIE" if record.get("outcome") == "ZOMBIE"
                                                 else "UNADMITTED_ABSENT")
                        self._unadmitted_absent[pid]["reconciliation_sequence"] = self._census_sequence
                        self._unadmitted_absent[pid]["reconciliation_census_sha256"] = _digest_bytes(_canonical(dict(fresh)))
                    if new_absences:
                        self._write_observed_containment()
                    if any(pid not in self._handles for pid in fresh_members):
                        members, census = fresh_members, fresh
                        continue
                    return fresh_members
                raise CustodyError("candidate reconciliation did not stabilize within its bound")
            except BaseException as error:
                self._uncertain = error
                raise

    def _register_census_candidate(self, pid, birth, *, census, root_pid, root_handle,
                                   actor, previously_admitted, census_round, new_absences,
                                   fresh_census=None, retry_admissions=None, _capture_retry=False,
                                   _retry_expected=None, _collected_observation=None):
        try:
            observation = (_collected_observation if _collected_observation is not None
                           else ordinary_candidate_observation(pid)) if pid not in self._handles else None
            if observation is not None and observation.get("outcome") == "LIVE":
                row = census.get(pid, {})
                identity = observation.get("identity", {})
                native = observation.get("native")
                fields = ("pid", "uid", "parent_pid", "pgid", "start_sec", "start_usec")
                if (identity.get("parent_pid") != row.get("parent_pid")
                        or (isinstance(native, Mapping)
                            and any(type(native.get(k)) is not int or native[k] != row.get(k) for k in fields))):
                    raise CustodyError("live candidate differs from observed parent edge or incarnation")
                ancestry_census = census
                if census.get(pid, {}).get("parent_pid") != root_pid and fresh_census is not None:
                    ancestry_census = fresh_census()
                    self._collect_census_candidates({}, ancestry_census)
                    original, current = census.get(pid), ancestry_census.get(pid)
                    fields = ("pid", "uid", "birth_id", "parent_pid", "pgid", "start_sec", "start_usec")
                    if (not isinstance(current, Mapping)
                            or any(original.get(k) != current.get(k) for k in fields)):
                        raise CustodyError("live candidate ancestry or incarnation changed")
                self._validate_candidate_chain(pid, birth, ancestry_census)
            if _capture_retry:
                native = observation.get('native') if isinstance(observation, Mapping) else None
                precise = ('pid', 'uid', 'parent_pid', 'pgid', 'start_sec', 'start_usec')
                if (not isinstance(observation, Mapping) or observation.get('outcome') != 'LIVE'
                        or not isinstance(native, Mapping) or not isinstance(_retry_expected, Mapping)
                        or _retry_expected.get('birth_id') != birth
                        or any(type(native.get(k)) is not int or type(_retry_expected.get(k)) is not int
                               or native[k] != _retry_expected[k] for k in precise)):
                    raise CustodyError('candidate changed before bounded capture retry')
            self.register(pid, birth, _observation=observation, _capture_retry=_capture_retry)
            if (observation is not None and observation.get("outcome") == "LIVE"
                    and census[pid]["parent_pid"] != root_pid and fresh_census is not None):
                after = fresh_census()
                self._collect_census_candidates({}, after)
                self._validate_candidate_chain(pid, birth, after)
        except CustodyError as error:
            cause = error.__cause__
            capture = getattr(cause, "capture_failure", None)
            observations = getattr(cause, "candidate_observations", None)
            if isinstance(cause, NativeTokenCaptureError) and observations is not None:
                census_fields = ("pid", "uid", "birth_id", "parent_pid", "pgid", "status", "start_sec", "start_usec")
                diagnostic = {"pid": pid, "birth_id": birth, "capture_failure": capture,
                              "observations": observations,
                              "census": {member: {key: row[key] for key in census_fields if key in row}
                                         for member, row in census.items()},
                              "census_sha256": _digest_bytes(_canonical(dict(census))),
                              "root": {"pid": root_pid, "birth_id": root_handle[0]},
                              "observer": {key: actor[key] for key in ("pid", "uid", "birth_id")},
                              "observation_sequence": self._census_sequence,
                              "census_round": census_round, "admitted": False}
                if getattr(self, "command_diagnostics", False):
                    commands = getattr(cause, "failure_command_observations", None)
                    try:
                        pre = observations.get("pre", {}).get("native")
                        post = observations.get("post", {}).get("native")
                        row = census.get(pid)
                        parent = commands.get("parent_pid") if isinstance(commands, Mapping) else None
                        # Every association must agree with BOTH precise capture
                        # observations and the original census, including UID.
                        parent_bound = (isinstance(pre, Mapping) and isinstance(post, Mapping)
                                        and type(parent) is int and parent > 0
                                        and pre.get("parent_pid") == post.get("parent_pid") == parent)
                        diagnostic["failure_command_observations"] = {
                            "candidate_pid": pid, "parent_pid": parent,
                            "target": _capture_failure_command_evidence(
                                commands.get("target") if isinstance(commands, Mapping) else None,
                                [pre, post, row]),
                            "parent": _capture_failure_command_evidence(
                                commands.get("parent") if isinstance(commands, Mapping) else None,
                                [census.get(parent)] if parent_bound else []),
                            "authority": "NONE",
                            "observation_error_type": commands.get("error_type") if isinstance(commands, Mapping) else None,
                        }
                        # A malformed optional augmentation must not prevent
                        # persistence of the mandatory capture-failure journal.
                        _canonical(diagnostic["failure_command_observations"])
                    except BaseException as diagnostic_error:
                        diagnostic["failure_command_observations"] = {
                            "classification": "UNPROVED", "authority": "NONE",
                            "error_type": type(diagnostic_error).__name__,
                        }
                self._capture_failure_observations = getattr(self, "_capture_failure_observations", []) + [diagnostic]
                try:
                    self._write_observed_containment()
                except BaseException as persistence_error:
                    error.evidence_error_type = type(persistence_error).__name__
                    raise CustodyError("capture-failure diagnostic persistence failed") from error
            # One synchronous retry can acquire actual authority; it never defers
            # an uncaptured process to another census or grants numeric authority.
            if _capture_retry:
                raise
            if (fresh_census is not None and retry_admissions is not None
                    and getattr(error, 'registration_stage', None) == 'token-capture'
                    and type(cause) is NativeTokenCaptureError
                    and isinstance(capture, Mapping) and isinstance(observations, Mapping)
                    and isinstance(observations.get('pre'), Mapping)
                    and isinstance(observations.get('post'), Mapping)
                    and observations['pre'].get('outcome') == observations['post'].get('outcome') == 'LIVE'
                    and capture.get('substage') == 'task_info_first'
                    and type(capture.get('kern_return')) is int
                    and capture['kern_return'] == 0x10000003
                    and capture.get('port_acquired') is True
                    and capture.get('port_release_succeeded') is True
                    and getattr(cause, 'port_release_error', None) is None):
                pre, post = observations.get('pre'), observations.get('post')
                row = census.get(pid)
                fields = ('pid', 'uid', 'parent_pid', 'pgid', 'start_sec', 'start_usec')
                def same_live(observed, expected):
                    native = observed.get('native') if isinstance(observed, Mapping) else None
                    return (isinstance(observed, Mapping) and observed.get('outcome') == 'LIVE'
                            and isinstance(native, Mapping) and isinstance(expected, Mapping)
                            and all(type(native.get(k)) is int and type(expected.get(k)) is int
                                    and native[k] == expected[k] for k in fields)
                            and native.get('status') in {1, 2, 3, 4})
                role = 'descendant_' + str(pid) + '_' + hashlib.sha256(birth.encode()).hexdigest()[:12]
                designation = self.root / ('designation-' + role + '.json')
                parent = row.get('parent_pid') if isinstance(row, Mapping) else None
                parent_handle = self._handles.get(parent)
                if (not same_live(pre, row) or not same_live(post, row)
                        or row.get('birth_id') != birth or row.get('uid') != actor['uid']
                        or _ps_lstart_birth(row['start_sec']) != birth
                        or pid == root_pid or pid in self._handles or pid in previously_admitted
                        or pid in self._unadmitted_absent
                        or parent not in previously_admitted or parent_handle is None
                        or designation.exists() or designation.is_symlink()
                        or any(value.get('parent_pid') == pid for value in census.values())
                        or self.actor.verify() != actor or self._handles.get(root_pid) != root_handle):
                    raise
                fresh = fresh_census()
                self._collect_census_candidates({}, fresh)
                self._check_unadmitted_census(fresh)
                current = fresh.get(pid)
                parent_row = fresh.get(parent)
                if (not isinstance(current, Mapping) or current.get('birth_id') != birth
                        or any(type(current.get(k)) is not int or current[k] != row[k] for k in fields)
                        or current.get('status') not in {1, 2, 3, 4}
                        or not isinstance(parent_row, Mapping)
                        or parent_row.get('birth_id') != parent_handle[0]
                        or parent_row.get('status') not in {1, 2, 3, 4}
                        or self._handles.get(parent) != parent_handle
                        or any(value.get('parent_pid') == pid for value in fresh.values())
                        or self.actor.verify() != actor or self._handles.get(root_pid) != root_handle):
                    raise
                retry_pre = ordinary_candidate_observation(pid)
                if not same_live(retry_pre, row) or retry_pre.get('identity') != pre.get('identity'):
                    raise
                self._register_census_candidate(pid, birth, census=fresh,
                    root_pid=root_pid, root_handle=root_handle, actor=actor,
                    previously_admitted=previously_admitted, census_round=census_round,
                    new_absences=new_absences, _capture_retry=True, _retry_expected=row)
                if (self.actor.verify() != actor or self._handles.get(root_pid) != root_handle):
                    raise CustodyError('candidate or retained owner changed during bounded capture retry') from error
                retry_admissions.append(pid)
                return
            initial = (getattr(error, "registration_stage", None) == "initial-identity"
                       and type(cause) in {UnadmittedCandidateAbsent, UnadmittedCandidateZombie})
            acquisition_failed = (getattr(error, "registration_stage", None) == "token-capture"
                              and type(cause) is NativeTokenCaptureError and observations is not None
                              and capture["substage"] in {"task_name_for_pid", "task_info_first",
                                                          "task_info_second", "pid_for_task"}
                              and type(capture["kern_return"]) is int and capture["kern_return"] != 0
                              and getattr(cause, "port_release_error", None) is None
                              and ((capture["substage"] == "task_name_for_pid" and capture["port_acquired"] is False)
                                   or (capture["port_acquired"] is True
                                       and capture.get("port_release_succeeded") is True)))
            post_token_terminal = (getattr(error, "registration_stage", None) == "post-token-identity"
                                   and type(cause) is _PostTokenCandidateTerminal)
            observed_capture = acquisition_failed or post_token_terminal
            captured_absent = (observed_capture and isinstance(observations.get("post"), Mapping)
                               and observations["post"].get("outcome") == "ABSENT")
            captured_zombie = observed_capture and not captured_absent
            role = "descendant_" + str(pid) + "_" + hashlib.sha256(birth.encode()).hexdigest()[:12]
            designation_path = self.root / ("designation-" + role + ".json")
            if (pid == root_pid or pid in self._handles or pid in previously_admitted
                    or not (initial or observed_capture)
                    or (observed_capture and (designation_path.exists() or designation_path.is_symlink()))):
                raise
            row = census.get(pid)
            if row is None or row.get("birth_id") != birth:
                raise CustodyError("unadmitted candidate lacks matching census evidence") from error
            terminal = captured_zombie or type(cause) is UnadmittedCandidateZombie
            native = observations["post"].get("native") if captured_zombie else (cause.observation["native"] if terminal else None)
            if observed_capture:
                if post_token_terminal and (cause.token_identity.get("pid") != pid
                                            or cause.token_identity.get("uid") != actor["uid"]):
                    raise CustodyError("captured token differs from candidate observer identity") from error
                if self.actor.verify() != actor or self._handles.get(root_pid) != root_handle:
                    raise CustodyError("capture-failure observer or retained root changed") from error
                pre, post = observations["pre"], observations["post"]
                fields = ("pid", "uid", "start_sec", "start_usec")
                if isinstance(post, Mapping) and post.get("outcome") == "LIVE":
                    raise CustodyError("post-capture candidate is LIVE; terminal disposition is ineligible") from error
                if (not isinstance(pre, Mapping) or pre.get("outcome") != "LIVE"
                        or not isinstance(pre.get("native"), Mapping)
                        or any(type(pre["native"].get(k)) is not int or type(row.get(k)) is not int
                               or pre["native"][k] != row[k] for k in fields)
                        or pre["native"]["pid"] != pid or pre["native"]["uid"] != actor["uid"]
                        or type(pre["native"].get("parent_pid")) is not int
                        or pre["native"]["parent_pid"] != row.get("parent_pid")
                        or pre["native"].get("status") not in {1, 2, 3, 4}
                        or _ps_lstart_birth(pre["native"]["start_sec"]) != birth
                        or not 0 <= pre["native"]["start_usec"] < 1000000):
                    raise CustodyError("pre-capture candidate differs from precise native census") from error
                if not captured_absent and (post.get("outcome") != "ZOMBIE"
                        or not isinstance(native, Mapping)
                        or any(type(native.get(k)) is not int or native[k] != row[k] for k in fields)
                        or native.get("parent_pid") != row.get("parent_pid")):
                    raise CustodyError("post-capture terminal observation is not the same precise native incarnation") from error
            if terminal:
                self._validate_terminal_observation(pid, birth, row, native, initial=True)
            ancestry = []
            parent = row["parent_pid"]
            seen = {pid}
            while parent not in self._handles:
                if parent in seen or parent not in census:
                    raise CustodyError("unadmitted candidate ancestry is unproved") from error
                seen.add(parent)
                ancestor = census[parent]
                if ancestor.get("status") == 5:
                    raise CustodyError("unadmitted candidate requires zombie-parent ancestry") from error
                ancestry.append({"pid": parent, **dict(ancestor)})
                parent = ancestor["parent_pid"]
            anchor = self._handles[parent][0]
            if (parent not in census or census[parent]["birth_id"] != anchor
                    or census[parent].get("status") == 5):
                raise CustodyError("unadmitted candidate ancestry anchor changed") from error
            self._unadmitted_absent[pid] = {
                "disposition": "PENDING_RECONCILIATION", "pid": pid,
                "birth_id": birth, "census_parent_pid": row["parent_pid"],
                "census_ancestry": ancestry,
                "admitted_ancestry_anchor": {"pid": parent, "birth_id": anchor},
                "root": {"pid": root_pid, "birth_id": root_handle[0]},
                "observer": {key: actor[key] for key in ("pid", "uid", "birth_id")},
                "stage": ("post-token-no-authority-absence" if post_token_terminal and captured_absent else
                          "post-token-identity" if post_token_terminal else
                          "token-capture-no-authority-absence" if captured_absent else
                          "token-capture" if captured_zombie else "initial-identity"),
                "observation_sequence": self._census_sequence,
                "census_round": census_round,
                "census_sha256": _digest_bytes(_canonical(dict(census))),
                "admitted": False, "capability": "NONE",
                "exit_reap": "UNPROVED", "subtree": "UNPROVED",
                "outcome": "ZOMBIE" if terminal else "ABSENT",
                "terminal_observation": native,
                "absence_observed": not terminal,
            }
            if captured_absent:
                # Successful absence is not a terminal certificate. Retain the
                # exact original ancestry for fresh, live revalidation before
                # completing this never-acquired disposition.
                record = self._unadmitted_absent[pid]
                record["reconciliation_ancestry"] = [dict(census[root_pid]),
                    *ancestry, *([] if parent == root_pid else [dict(census[parent])])]
                self._revalidate_post_absence(census, [pid], actor, root_pid, root_handle)
            self._write_observed_containment()
            new_absences.append(pid)

    def _revalidate_post_absence(self, census, candidates, actor, root_pid, root_handle):
        records = [self._unadmitted_absent[pid] for pid in candidates
                   if self._unadmitted_absent[pid]["stage"] in {
                       "token-capture-no-authority-absence", "post-token-no-authority-absence"}]
        if not records:
            return
        if self.actor.verify() != actor or self._handles.get(root_pid) != root_handle:
            raise CustodyError("post-absence observer or retained root changed")
        fields = ("pid", "uid", "start_sec", "start_usec", "parent_pid")
        for record in records:
            for original in record["reconciliation_ancestry"]:
                pid = original.get("pid")
                current = census.get(pid)
                if (not isinstance(current, Mapping)
                        or any(type(original.get(k)) is not int or type(current.get(k)) is not int
                               or original[k] != current[k] for k in fields)
                        or original["uid"] != actor["uid"] or original["start_sec"] <= 0
                        or not 0 <= original["start_usec"] < 1000000
                        or original.get("status") not in {1, 2, 3, 4}
                        or current.get("status") not in {1, 2, 3, 4}
                        or original.get("birth_id") != current.get("birth_id")
                        or _ps_lstart_birth(original["start_sec"]) != original.get("birth_id")
                        or (pid == root_pid and current.get("birth_id") != root_handle[0])):
                    raise CustodyError("post-absence live root or ancestry is unproved")

    @staticmethod
    def _validate_terminal_observation(pid, birth, census_row, native, *, initial=False):
        # Precise native birth is evidence only; never replace canonical authority IDs.
        fields = ("status", "start_sec", "start_usec")
        if (not isinstance(native, Mapping) or type(native.get("pid")) is not int or native["pid"] != pid
                or any(type(native.get(k)) is not int for k in fields)
                or native["status"] != 5 or native["start_sec"] <= 0
                or not 0 <= native["start_usec"] < 1000000
                or _ps_lstart_birth(native["start_sec"]) != birth
                or census_row.get("birth_id") != birth
                or any(type(census_row.get(k)) is not int for k in fields)
                or census_row["start_sec"] != native["start_sec"]
                or census_row["start_usec"] != native["start_usec"]
                or census_row["status"] not in ({1, 2, 3, 4, 5} if initial else {5})):
            raise CustodyError("terminal candidate birth/status observation contradicts retained census")

    def signal(self, pid: int, birth: str, signum: int, *, cleanup_deadline: float | None = None) -> None:
        with self._lock:
            self._verify_root()
            entry = self._handles.get(pid)
            if entry is None or entry[0] != birth:
                raise CustodyError("signal lacks a retained exact process capability")
            handle = entry[1]
            if isinstance(handle, RoleBoundCustodyBroker):
                try:
                    handle.signal(signum, expected_pid=pid)
                except (_SignalTargetGone, _SignalCaptureTargetGone) as exc:
                    if (handle is not self._launch_broker or self.process is None
                            or pid != self.process.pid):
                        raise
                    remaining = (0.0 if cleanup_deadline is None else
                                 max(0.0, cleanup_deadline - time.monotonic()))
                    try:
                        self.process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired as timeout:
                        raise exc from timeout
                except (DarwinProcessExitedError, AuditTokenSignalError) as exc:
                    if (isinstance(exc, AuditTokenSignalError) and exc.result != errno.ESRCH):
                        raise
                    if self.process is None or pid != self.process.pid or self.process.poll() is None:
                        raise
                    # Only the retained direct child has positive wait/reap
                    # evidence. Its exit settles this signal race, never the
                    # separately retained descendants or census uncertainty.
            elif isinstance(handle, tuple):
                authority, reference = handle
                try:
                    refreshed = authority.signal(actor=self.actor, generation=reference["generation"],
                                                 expected_target=reference["target"], signum=signum,
                                                 identity_provider=default_process_identity)
                except _SignalTargetGone:
                    if (not isinstance(self._launch_broker, RoleBoundCustodyBroker)
                            or self._launch_broker.role != "command"
                            or self.process is None or pid == self.process.pid
                            or authority.role != f"descendant_{pid}_{hashlib.sha256(birth.encode()).hexdigest()[:12]}"):
                        raise
                    # No kernel call was attempted. Retain the exact capability;
                    # census, original waits and final absence still decide cleanup.
                    return
                self._handles[pid] = (birth, (authority, refreshed))
            else:
                from astrid.core.execution.process_group import process_birth_identity
                if self._actor_birth != process_birth_identity():
                    raise CustodyError("cleanup actor incarnation changed")
                try:
                    signal.pidfd_send_signal(handle, signum, None, 0)
                except ProcessLookupError:
                    return
                except OSError as exc:
                    raise CustodyError("retained pidfd signal failed") from exc

    def observe(self, members: Mapping[int, str]) -> None:
        with self._lock:
            if self._uncertain is not None:
                raise CustodyError("custody registration or observation is unresolved") from self._uncertain
            try:
                for pid, birth in members.items():
                    self.register(pid, birth)
            except BaseException as exc:
                self._uncertain = exc
                raise

    def start_observer(self) -> None:
        """Complete one census before launch release, then watch descendants."""
        ready = threading.Event()

        def monitor() -> None:
            from astrid.core.execution.process_group import observe_tree
            while True:
                try:
                    live = observe_tree(self.process)
                    if not ready.is_set():
                        if self.process.pid not in live:
                            raise CustodyError("initial custody census omitted the suspended launch")
                        ready.set()
                    if not live:
                        return
                except BaseException as exc:
                    with self._lock:
                        self._uncertain = exc
                    self.process._astrid_tree_uncertain = str(exc) or type(exc).__name__
                    ready.set()
                    return
                time.sleep(0.01)
        self._observer = threading.Thread(target=monitor, name="astrid-custody-observer", daemon=True)
        self._observer.start()
        timeout = self._launch_broker.timeout if self._launch_broker is not None else 1.0
        if not ready.wait(timeout):
            raise CustodyError("initial custody observer census timed out")
        if self._uncertain is not None:
            raise CustodyError("initial custody observer census is unresolved") from self._uncertain

    def release_launch(self) -> None:
        """Release a Darwin launch only after bind and observer initialization."""

        if self._launch_broker is None:
            return
        if self.process is None:
            raise CustodyError("suspended launch has no retained child")
        self._launch_broker.release_suspended_launch(expected_pid=self.process.pid, process=self.process)
        if getattr(self, "_inventory_owner", None) is not None:
            self._inventory_owner.released.set()

    def cleanup_failed_launch(self, *, signum: int) -> None:
        if getattr(self, "_inventory_owner", None) is not None:
            self._inventory_owner.abort_before_release()
        # Failed admission obeys the same actor/generation fence. A pending
        # token or Popen PID never authorizes a destructive fallback.
        if self.process is None:
            if self._launch_broker is not None:
                self._launch_broker.abort_before_spawn()
            return
        if self._launch_broker is None:
            for pid, (birth, _) in reversed(tuple(self._handles.items())):
                self.signal(pid, birth, signum)
            if not self._handles:
                raise CustodyError("failed launch has unresolved kernel custody")
            return
        suspended = (
            self._launch_broker._post_exec_authority_validated
            and not self._launch_broker._release_sent
        )
        if suspended and self._launch_broker.error is None:
            self._launch_broker.error = self._uncertain or CustodyError(
                "launch admission aborted before the suspended target was released"
            )
        # TERM cannot settle a stopped target. Never resume unadmitted code;
        # KILL still crosses the same exact-authority and actor/generation fence.
        self._launch_broker.signal_failed_admission(
            signal.SIGKILL if suspended else signum, expected_pid=self.process.pid,
        )
        if suspended:
            # Only the original Popen supplies positive direct-child reap evidence.
            # Its returncode retains the result; timeout leaves custody unresolved.
            self.process.wait(timeout=_FAILED_LAUNCH_REAP_TIMEOUT_SECONDS)
            self.settle_launch()


ENGINE_CONTROL_VERSION = "runtime.local-execution-engine-control/v1"
ENGINE_CONTROL_SIGNAL_V2 = "runtime.local-execution-engine-control/v2"


def signal_owned_listener(request: Mapping[str, Any], *, engine_actor: Any,
                          launch_parent: Any, listener: RoleBoundCustodyBroker,
                          operation_id: str, channel_id: str, owner_epoch: str) -> dict[str, Any]:
    """Consume Runtime's bounded native signal ABI as the designated host."""
    required = {"version", "command", "operation_id", "channel_id", "owner_epoch", "role", "generation", "target", "signal"}
    if request.get("version") == ENGINE_CONTROL_SIGNAL_V2:
        target = request.get("target")
        if (set(request) != required or request.get("command") != "signal_owned_listener"
                or request.get("role") != "engine_listener"
                or any(request.get(k) != v for k, v in {"operation_id": operation_id, "channel_id": channel_id, "owner_epoch": owner_epoch}.items())
                or type(request.get("signal")) is not int or request["signal"] not in {15, 9}
                or type(request.get("generation")) is not int or request["generation"] < 1
                or not isinstance(target, dict)
                or set(target) != {"pid", "uid", "birth_id", "audit_token_sha256", "audit_token_pidversion"}
                or type(target.get("pid")) is not int or target["pid"] < 1
                or type(target.get("uid")) is not int or target["uid"] < 0
                or not isinstance(target.get("birth_id"), str) or not target["birth_id"]
                or type(target.get("audit_token_pidversion")) is not int or target["audit_token_pidversion"] < 1
                or not isinstance(target.get("audit_token_sha256"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", target["audit_token_sha256"]) is None):
            raise CustodyError("native listener signal v2 has an invalid binding")
        if engine_actor.verify() != launch_parent.verify():
            raise CustodyError("native listener signal came from a foreign engine")
        current = listener.role_authority.signal(
            actor=listener.cleanup_actor, generation=request["generation"],
            expected_target=target, signum=request["signal"], identity_provider=listener.identity_provider)
        # A bad returned reference is an uncertain successful kernel outcome,
        # never permission to acknowledge a different process or transfer.
        if (not isinstance(current, Mapping) or type(current.get("generation")) is not int
                or current.get("generation") != request["generation"]
                or not isinstance(current.get("target"), Mapping)
                or _canonical({k: current["target"].get(k) for k in ("pid", "uid", "birth_id")})
                != _canonical({k: target[k] for k in ("pid", "uid", "birth_id")})):
            raise CustodyError("native listener signal returned conflicting custody")
        return {**{k: v for k, v in request.items() if k != "command"},
                "status": "signaled", "current_reference": current}
    if (set(request) != required or request.get("version") != ENGINE_CONTROL_VERSION
            or request.get("command") != "signal_owned_listener"
            or request.get("role") != "engine_listener"
            or any(request.get(k) != v for k, v in {"operation_id": operation_id, "channel_id": channel_id, "owner_epoch": owner_epoch}.items())
            or isinstance(request.get("signal"), bool) or request.get("signal") not in {15, 9}):
        raise CustodyError("native listener signal has an invalid binding")
    if engine_actor.verify() != launch_parent.verify():
        raise CustodyError("native listener signal came from a foreign engine")
    listener.role_authority.signal(actor=listener.cleanup_actor, generation=request["generation"],
                                   expected_target=request["target"], signum=request["signal"],
                                   identity_provider=listener.identity_provider)
    return {**{k: v for k, v in request.items() if k != "command"}, "status": "signaled"}


if __name__ == "__main__":
    if sys.argv[1:] == ["--custody-exec"]:
        raise SystemExit(child_exec_from_environment())
    raise SystemExit("private custody wrapper requires --custody-exec")
