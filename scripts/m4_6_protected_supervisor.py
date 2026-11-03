"""Collector-owned Linux diagnostics for the still-unavailable protected seam.

Only an inert, source-fixed helper can be launched here. No caller identity,
snapshot, command, inner exit or settlement assertion is accepted. These local
records cannot authenticate candidate/run/plan scope or survive collector loss.
They are not a distinct-principal channel, installer grant or protected export.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import select
import subprocess
import sys
import threading
import time

from .m4_6_process_observer import (
    NativeServiceObservationTarget, ProcessSnapshot, ProcessTransitionObserver,
    _digest_file,
)


class SupervisorRefused(RuntimeError):
    """A diagnostic is incomplete, or the protected interface is unavailable."""


def _encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


@dataclass(frozen=True)
class LinuxProcessRecord:
    pid: int
    birth: str
    uids: tuple[int, ...]
    executable: str
    executable_sha256: str
    argv: tuple[str, ...]
    cwd: str
    parent_pid: int


def _read_process(pid: int) -> LinuxProcessRecord:
    """Private OS reader; no caller-provided process anchor is used."""
    root = Path(f"/proc/{pid}")
    raw = (root / "stat").read_text()
    end = raw.rfind(") ")
    fields = raw[end + 2:].split() if end >= 0 else []
    if (not raw.startswith(f"{pid} (") or len(fields) < 20 or
        fields[0] in {"Z", "X", "x"} or not fields[1].isdecimal() or
        not fields[19].isdecimal() or int(fields[19]) <= 0):
        raise SupervisorRefused("malformed or exited process birth identity")
    uid_rows = [line.split()[1:] for line in (root / "status").read_text().splitlines()
                if line.startswith("Uid:")]
    if (len(uid_rows) != 1 or len(uid_rows[0]) != 4 or
        any(not item.isdecimal() for item in uid_rows[0])):
        raise SupervisorRefused("malformed process UID identity")
    uids = tuple(int(item) for item in uid_rows[0])
    if len(set(uids)) != 1:
        raise SupervisorRefused("mixed process UID identity")
    executable = os.readlink(root / "exe")
    cwd = os.readlink(root / "cwd")
    command = (root / "cmdline").read_bytes()
    if (not command.endswith(b"\0") or not command[:-1] or
        not Path(executable).is_absolute() or executable.endswith(" (deleted)") or
        not Path(cwd).is_absolute() or cwd.endswith(" (deleted)")):
        raise SupervisorRefused("malformed executable, argv or cwd identity")
    return LinuxProcessRecord(pid, fields[19], uids, executable,
                              _digest_file(root / "exe"),
                              tuple(os.fsdecode(arg) for arg in command[:-1].split(b"\0")),
                              cwd, int(fields[1]))


class _PinnedProcess:
    """Keep a kernel process handle while comparing two bounded OS reads.

    This detects observed drift, not exec-and-return between reads or descendant
    escape. No lifecycle/settlement authority is inferred from the handle.
    """

    def __init__(self, pid: int) -> None:
        if sys.platform != "linux" or type(pid) is not int or pid <= 0:
            raise SupervisorRefused("Linux positive PID is required")
        self.pid = pid
        self.fd = os.pidfd_open(pid)

    def capture(self) -> LinuxProcessRecord:
        info = Path(f"/proc/self/fdinfo/{self.fd}").read_text().splitlines()
        pids = [line.split(":", 1)[1].strip() for line in info if line.startswith("Pid:")]
        if pids != [str(self.pid)] or select.select([self.fd], [], [], 0)[0]:
            raise SupervisorRefused("pinned process exited or changed")
        first = _read_process(self.pid)
        second = _read_process(self.pid)
        if first != second or select.select([self.fd], [], [], 0)[0]:
            raise SupervisorRefused("process identity changed during acquisition")
        return first

    def close(self) -> None:
        os.close(self.fd)


def observe_self() -> LinuxProcessRecord:
    """Acquire the collector's actual identity, including UID zero if applicable."""
    pin = _PinnedProcess(os.getpid())
    try:
        return pin.capture()
    finally:
        pin.close()


# No product command, installation path or operation can be supplied to this
# helper. Its reply is diagnostic correlation only; it never reports settlement.
_HELPER = """import hashlib, json, sys
sys.stdout.buffer.write(b'ready\\n'); sys.stdout.buffer.flush()
request = sys.stdin.buffer.readline(4097)
if len(request) != 33 or request[-1:] != b'\\n':
    raise SystemExit(65)
print(json.dumps({'diagnostic_request_sha256': hashlib.sha256(request[:-1]).hexdigest()},
                 sort_keys=True, separators=(',', ':')), flush=True)
"""


class _OutputCapture:
    """Bound both memory and waits, including partial readiness and inherited pipes."""

    def __init__(self, child: subprocess.Popen) -> None:
        self.child = child
        self.streams = {child.stdout.fileno(): bytearray(), child.stderr.fileno(): bytearray()}
        self.open_fds = set(self.streams)
        self.complete = False

    def _poll(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SupervisorRefused("fixed helper output timed out")
        ready = select.select(list(self.open_fds), [], [], remaining)[0]
        if not ready:
            raise SupervisorRefused("fixed helper output timed out")
        for fd in ready:
            chunk = os.read(fd, 4096)
            if not chunk:
                self.open_fds.remove(fd)
            else:
                capacity = 65536 - len(self.streams[fd])
                self.streams[fd].extend(chunk[:capacity])
                if len(chunk) > capacity:
                    raise SupervisorRefused("fixed helper output exceeds diagnostic bound")

    def ready(self) -> None:
        deadline = time.monotonic() + 5
        while len(self.stdout) < 6 and self.open_fds:
            self._poll(deadline)
        if self.stdout != b"ready\n" or self.stderr:
            raise SupervisorRefused("fixed helper readiness incomplete")

    def finish(self) -> None:
        deadline = time.monotonic() + 5
        while self.open_fds:
            self._poll(deadline)
        self.child.wait(timeout=max(0.001, deadline - time.monotonic()))
        self.complete = True

    @property
    def stdout(self) -> bytes:
        return bytes(self.streams[self.child.stdout.fileno()])

    @property
    def stderr(self) -> bytes:
        return bytes(self.streams[self.child.stderr.fileno()])


class DiagnosticCheckpointCollector:
    """Own an observer, one local request and one actual fixed helper child.

    ``target`` is only a diagnostic expectation, never authenticated scope. The
    observer runs as this collector's UID, including systemctl --user. A root
    collector therefore cannot use it as a bridge to another UID's manager.
    Recreating this object cannot resume prior history or provide durable replay
    protection. Records remain ordinary same-principal in-memory diagnostics.
    """

    def __init__(self, target: NativeServiceObservationTarget) -> None:
        if type(target) is not NativeServiceObservationTarget or target.os_name != "linux":
            raise SupervisorRefused("exact Linux observation target required")
        self._collector = observe_self()
        self._target = target
        self._observer = ProcessTransitionObserver(target)
        self._state = "created"
        self._records: list[bytes] = []
        self._lock = threading.Lock()
        self._append("collector", asdict(self._collector))

    def _append(self, kind: str, value: object) -> None:
        self._records.append(_encode({"kind": kind, "value": value}))

    def _retain_snapshot(self, kind: str, snapshot: ProcessSnapshot) -> None:
        if (type(snapshot) is not ProcessSnapshot or
            snapshot.source != "linux-bounded-proc-systemd-poll" or
            snapshot.manager.manager != "systemd-user" or
            snapshot.manager.target != self._target.manager_target):
            raise SupervisorRefused("observer source or target differs")
        self._append(kind, snapshot.record())

    def _snapshot(self, kind: str) -> None:
        self._retain_snapshot(kind, self._observer.poll())

    @property
    def report_bytes(self) -> bytes:
        with self._lock:
            return _encode({"authority": "none", "admission": "refused",
                            "scope": "same-principal-inert-helper-diagnostic",
                            "state": self._state, "inner_uninstall_exit": None,
                            "settlement": "unavailable",
                            "records": [{"sha256": hashlib.sha256(row).hexdigest(),
                                         "record": json.loads(row)} for row in self._records]})

    def arm_initial_absence(self) -> None:
        with self._lock:
            if self._state != "created":
                raise SupervisorRefused("collector arm already attempted")
            self._state = "refused"
            try:
                self._observer.arm_initial_absence()
                self._retain_snapshot("initial-absence-checkpoint", self._observer.latest_snapshot)
                self._state = "armed"
            except Exception as exc:
                self._append("arm-failed", {"error": str(exc)})
                try:
                    self._observer.close("diagnostic arm failed")
                except Exception as close_error:
                    self._append("observer-close-failed", {"error": str(close_error)})
                raise SupervisorRefused("initial absence unavailable") from exc

    def collect_diagnostic(self) -> bytes:
        """Consume before launching; retain initial/final polls even on failure.

        These polls bracket an inert helper, not a product removal or settled
        inner operation. In particular a final poll is not finalization.
        """
        with self._lock:
            if self._state != "armed":
                raise SupervisorRefused("collector is unarmed or already consumed")
            self._state = "consumed"
            child = None
            pin = None
            output = None
            error = None
            def failed(kind: str, exc: Exception) -> None:
                nonlocal error
                error = error or exc
                self._state = "refused"
                self._append(kind, {"error": str(exc)})
            try:
                if observe_self() != self._collector:
                    raise SupervisorRefused("collector identity differs")
                self._snapshot("initial-diagnostic-checkpoint")
                request = secrets.token_hex(16).encode("ascii")
                self._append("diagnostic-request", {"request_id": request.decode(),
                             "sha256": hashlib.sha256(request).hexdigest()})
                argv = (self._collector.executable, "-I", "-S", "-c", _HELPER)
                child = subprocess.Popen(argv, cwd=self._collector.cwd,
                                         env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
                self._append("launched-pid", child.pid)
                output = _OutputCapture(child)
                pin = _PinnedProcess(child.pid)
                output.ready()
                identity = pin.capture()
                self._append("child", asdict(identity))
                if (identity.parent_pid != self._collector.pid or
                    identity.uids != self._collector.uids or
                    identity.executable != argv[0] or
                    identity.executable_sha256 != self._collector.executable_sha256 or
                    identity.argv != argv or identity.cwd != self._collector.cwd):
                    raise SupervisorRefused("actual child identity differs from fixed launch")
                child.stdin.write(request + b"\n")
                child.stdin.close()
                output.finish()
                expected = b"ready\n" + _encode({"diagnostic_request_sha256": hashlib.sha256(request).hexdigest()}) + b"\n"
                if child.returncode != 0 or output.stderr or output.stdout != expected:
                    raise SupervisorRefused("helper exit or exact request correlation differs")
                if observe_self() != self._collector:
                    raise SupervisorRefused("collector identity changed")
                self._state = "diagnostic-complete"
            except Exception as exc:
                failed("diagnostic-failed", exc)
            finally:
                if child is not None:
                    try:
                        if child.poll() is None:
                            child.kill()
                        child.wait(timeout=5)
                    except Exception as exc:
                        failed("child-cleanup-failed", exc)
                    stdout = output.stdout if output is not None else b""
                    stderr = output.stderr if output is not None else b""
                    self._append("child-result", {"outer_exit": child.returncode,
                                 "stdout_hex": stdout.hex(), "stderr_hex": stderr.hex(),
                                 "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                                 "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                                 "output_complete": output.complete if output is not None else False})
                    for stream in (child.stdin, child.stdout, child.stderr):
                        try:
                            stream.close()
                        except Exception as exc:
                            failed("pipe-cleanup-failed", exc)
                if pin is not None:
                    try:
                        pin.close()
                    except Exception as exc:
                        failed("pidfd-cleanup-failed", exc)
                try:
                    self._snapshot("final-diagnostic-checkpoint")
                except Exception as exc:
                    failed("final-observation-failed", exc)
                try:
                    self._observer.close("diagnostic complete; protected settlement unavailable")
                except Exception as exc:
                    failed("observer-close-failed", exc)
            if error is not None:
                raise SupervisorRefused("diagnostic incomplete; retained records available") from error
        return self.report_bytes

    def finalize_removal(self) -> None:
        raise SupervisorRefused("protected channel, real inner uninstall exit and settlement unavailable")


def open_protected_supervisor() -> None:
    """Fixed production seam: no configuration or serialized identity opens it."""
    raise SupervisorRefused("authenticated producer/installer/settlement/uploader channel unavailable")
