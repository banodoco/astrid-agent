"""Fail-closed, OS-owned process snapshots for the M4-6 native-service slot.

This module deliberately does *not* accept a Q/driver process census, a
caller-supplied process probe, or a producer-created digest.  It reads the
native manager and the operating system itself.  The currently implemented
sources are bounded polling snapshots, not lossless event streams.  Therefore
they retain useful local diagnostics but are never sufficient to authorize a
removal or finalization: every ledger begins with an explicit continuity-
unknown record.

The ledger is process-local and append-only.  It is intentionally not
serializable or re-hydratable; a new observer cannot resume another observer's
history and records that discontinuity as unknown.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping

_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_MAX_EXECUTABLE_BYTES: Final = 512 * 1024 * 1024
_MANAGER_BY_OS: Final = {
    "linux": "systemd-user",
    "macos": "launchd",
    "windows": "task-scheduler",
}


class ObservationRefused(RuntimeError):
    """An OS fact is unavailable, ambiguous, or cannot support authorization."""


class ObservationUnavailable(ObservationRefused):
    """The private source could not make one bounded OS observation."""


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest_file(path: Path) -> str:
    """Hash an observer-opened executable descriptor with a hard size bound."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ObservationUnavailable(f"executable cannot be opened: {path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size < 0
            or file_stat.st_size > _MAX_EXECUTABLE_BYTES
        ):
            raise ObservationUnavailable("executable is not a bounded regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise ObservationUnavailable(f"executable cannot be read: {path}") from exc
    finally:
        os.close(descriptor)


def _current_os_name() -> str:
    system = platform.system()
    names = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}
    try:
        return names[system]
    except KeyError as exc:
        raise ObservationUnavailable(f"unsupported host OS: {system}") from exc


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise ObservationUnavailable(f"path cannot be resolved: {path}") from exc


@dataclass(frozen=True)
class NativeServiceObservationTarget:
    """Already-bound native target facts, never a source of process observations."""

    os_name: str
    manager: str
    manager_target: str
    definition_path: Path | None
    allowed_executables: tuple[Path, ...]

    def __post_init__(self) -> None:
        if self.os_name != _current_os_name():
            raise ObservationUnavailable("observation target OS differs from the running host")
        if self.manager != _MANAGER_BY_OS[self.os_name]:
            raise ObservationUnavailable("observation target manager differs from the running host")
        if not self.manager_target.strip():
            raise ObservationUnavailable("native manager target is empty")
        if not self.allowed_executables:
            raise ObservationUnavailable("closed executable set is empty")
        object.__setattr__(
            self,
            "allowed_executables",
            tuple(_resolved(Path(path)) for path in self.allowed_executables),
        )
        if self.definition_path is not None:
            definition = Path(self.definition_path)
            if not definition.is_absolute():
                raise ObservationUnavailable("native definition path is not absolute")
            object.__setattr__(self, "definition_path", definition)


@dataclass(frozen=True)
class ProcessIdentity:
    """A non-PID-only process identity read by this observer from the OS."""

    pid: int
    birth_id: str
    executable_path: str
    executable_sha256: str
    parent_pid: int | None
    parent_birth_id: str | None
    discovery_id: str

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ObservationUnavailable("positive process PID is required")
        if not self.birth_id or not self.discovery_id:
            raise ObservationUnavailable("process birth/discovery identity is absent")
        if not Path(self.executable_path).is_absolute() or not _DIGEST_RE.fullmatch(
            self.executable_sha256
        ):
            raise ObservationUnavailable("process executable identity is invalid")
        if self.parent_pid is not None and (
            type(self.parent_pid) is not int or self.parent_pid <= 0
        ):
            raise ObservationUnavailable("process parent PID is invalid")
        if self.parent_pid is None and self.parent_birth_id is not None:
            raise ObservationUnavailable("parent birth identity requires a parent PID")

    @property
    def key(self) -> tuple[int, str]:
        return (self.pid, self.birth_id)

    def record(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "birth_id": self.birth_id,
            "executable_path": self.executable_path,
            "executable_sha256": self.executable_sha256,
            "parent_pid": self.parent_pid,
            "parent_birth_id": self.parent_birth_id,
            "discovery_id": self.discovery_id,
        }


@dataclass(frozen=True)
class ManagerIdentity:
    """Native manager target fact, independent from a driver/owner snapshot."""

    manager: str
    target: str
    state: str
    instance_id: str | None
    definition_path: str | None
    runtime_pid: int | None

    def __post_init__(self) -> None:
        if not self.manager or not self.target or self.state not in {"absent", "present"}:
            raise ObservationUnavailable("manager identity is malformed")
        if self.state == "absent" and (
            self.instance_id is not None
            or self.definition_path is not None
            or self.runtime_pid is not None
        ):
            raise ObservationUnavailable("absent manager carries live identity")
        if self.runtime_pid is not None and (
            type(self.runtime_pid) is not int or self.runtime_pid <= 0
        ):
            raise ObservationUnavailable("manager runtime PID is invalid")

    def record(self) -> dict[str, object]:
        return {
            "manager": self.manager,
            "target": self.target,
            "state": self.state,
            "instance_id": self.instance_id,
            "definition_path": self.definition_path,
            "runtime_pid": self.runtime_pid,
        }


@dataclass(frozen=True)
class ProcessSnapshot:
    """One bounded observation; it intentionally makes no continuity claim."""

    source: str
    manager: ManagerIdentity
    runtime: ProcessIdentity | None
    descendants: tuple[ProcessIdentity, ...]
    unbound_processes: tuple[ProcessIdentity, ...]
    source_sequence: int | None = None

    def __post_init__(self) -> None:
        if not self.source:
            raise ObservationUnavailable("observation source is absent")
        if self.source_sequence is not None and (
            type(self.source_sequence) is not int or self.source_sequence < 0
        ):
            raise ObservationUnavailable("event-source sequence is invalid")
        identities = self.identities
        if len({identity.key for identity in identities}) != len(identities):
            raise ObservationUnavailable("process snapshot has duplicate PID/birth identities")
        if self.manager.runtime_pid is None and self.runtime is not None:
            raise ObservationUnavailable("runtime exists without a manager runtime PID")
        if self.runtime is not None and self.manager.runtime_pid != self.runtime.pid:
            raise ObservationUnavailable("manager and Runtime PID differ")

    @property
    def identities(self) -> tuple[ProcessIdentity, ...]:
        runtime = () if self.runtime is None else (self.runtime,)
        return runtime + self.descendants + self.unbound_processes

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.record()).encode()).hexdigest()

    def record(self) -> dict[str, object]:
        return {
            "source": self.source,
            "manager": self.manager.record(),
            "runtime": None if self.runtime is None else self.runtime.record(),
            "descendants": [identity.record() for identity in self.descendants],
            "unbound_processes": [identity.record() for identity in self.unbound_processes],
            "source_sequence": self.source_sequence,
        }


@dataclass(frozen=True)
class LedgerEvent:
    """Immutable ledger row. ``detail_json`` prevents mutable detail aliases."""

    sequence: int
    monotonic_ns: int
    kind: str
    status: str
    detail_json: str

    @property
    def details(self) -> Mapping[str, object]:
        value = json.loads(self.detail_json)
        if not isinstance(value, dict):  # Defensive: only _TransitionLedger creates events.
            raise ObservationUnavailable("ledger detail is not an object")
        return value


class _TransitionLedger:
    """Session-private bounded append-only storage; no restore/replay operation exists."""

    def __init__(self, max_events: int) -> None:
        if type(max_events) is not int or max_events < 8:
            raise ValueError("max_events must leave room for an explicit overflow event")
        self._max_events = max_events
        self._events: list[LedgerEvent] = []
        self._next_sequence = 1
        self._overflowed = False

    def append(self, kind: str, status: str, details: Mapping[str, object]) -> None:
        if self._overflowed:
            return
        if len(self._events) >= self._max_events - 1:
            self._append_raw(
                "ledger-overflow",
                "overflow",
                {"max_events": self._max_events, "retention": "refuse"},
            )
            self._overflowed = True
            return
        self._append_raw(kind, status, details)

    def _append_raw(self, kind: str, status: str, details: Mapping[str, object]) -> None:
        self._events.append(
            LedgerEvent(
                sequence=self._next_sequence,
                monotonic_ns=time.monotonic_ns(),
                kind=kind,
                status=status,
                detail_json=_canonical(dict(details)),
            )
        )
        self._next_sequence += 1

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)


class _NativeSnapshotSource:
    """Base class for non-injectable, direct OS observation sources."""

    source_name: str

    def __init__(self, target: NativeServiceObservationTarget, max_processes: int) -> None:
        self.target = target
        self.max_processes = max_processes

    def capture(self) -> ProcessSnapshot:
        raise NotImplementedError

    def _identity(
        self,
        *,
        pid: int,
        birth_id: str,
        executable: Path,
        parent_pid: int | None,
        parent_birth_id: str | None,
        discovery_id: str,
    ) -> ProcessIdentity:
        resolved = _resolved(executable)
        if not any(
            _same_path(resolved, permitted) for permitted in self.target.allowed_executables
        ):
            raise ObservationUnavailable("process executable is outside the closed artifact set")
        return ProcessIdentity(
            pid=pid,
            birth_id=birth_id,
            executable_path=str(resolved),
            executable_sha256=_digest_file(resolved),
            parent_pid=parent_pid,
            parent_birth_id=parent_birth_id,
            discovery_id=discovery_id,
        )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


class _LinuxSnapshotSource(_NativeSnapshotSource):
    source_name = "linux-bounded-proc-systemd-poll"

    @staticmethod
    def _stat(pid: int) -> tuple[int, str]:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except (FileNotFoundError, ProcessLookupError) as exc:
            raise ObservationUnavailable("process vanished during /proc observation") from exc
        except OSError as exc:
            raise ObservationUnavailable("/proc process observation was denied") from exc
        separator = raw.rfind(")")
        if separator < 0:
            raise ObservationUnavailable("/proc stat record is malformed")
        fields = raw[separator + 2 :].split()
        if len(fields) <= 19 or not fields[1].isdigit() or not fields[19].isdigit():
            raise ObservationUnavailable("/proc process birth/parent is unavailable")
        return int(fields[1]), fields[19]

    def _table(self) -> dict[int, tuple[int, str]]:
        table: dict[int, tuple[int, str]] = {}
        try:
            entries = list(Path("/proc").iterdir())
        except OSError as exc:
            raise ObservationUnavailable("/proc cannot be enumerated") from exc
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                table[int(entry.name)] = self._stat(int(entry.name))
            except ObservationUnavailable as exc:
                if "vanished" in str(exc):
                    continue
                raise
        if len(table) > self.max_processes:
            raise ObservationUnavailable("process table exceeds bounded observation capacity")
        return table

    @staticmethod
    def _exe(pid: int) -> Path:
        try:
            return Path(f"/proc/{pid}/exe").resolve(strict=True)
        except (FileNotFoundError, ProcessLookupError) as exc:
            raise ObservationUnavailable("process executable vanished during observation") from exc
        except OSError as exc:
            raise ObservationUnavailable("process executable observation was denied") from exc

    def _process_identity(self, pid: int, table: Mapping[int, tuple[int, str]]) -> ProcessIdentity:
        try:
            parent_pid, birth = table[pid]
        except KeyError as exc:
            raise ObservationUnavailable("process escaped the bounded process table") from exc
        parent_birth = table[parent_pid][1] if parent_pid in table else None
        return self._identity(
            pid=pid,
            birth_id=birth,
            executable=self._exe(pid),
            parent_pid=parent_pid if parent_pid in table else None,
            parent_birth_id=parent_birth,
            discovery_id=f"linux-proc-v1:{pid}:{birth}",
        )

    def _manager(self) -> ManagerIdentity:
        try:
            completed = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    self.target.manager_target,
                    "--property=Id,LoadState,ActiveState,SubState,MainPID,InvocationID,FragmentPath",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ObservationUnavailable("systemd-user manager query is unavailable") from exc
        if completed.returncode not in {0, 4}:
            raise ObservationUnavailable("systemd-user manager query failed")
        fields: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in fields:
                raise ObservationUnavailable("systemd-user manager query has duplicate fields")
            fields[key] = value
        if fields.get("LoadState") == "not-found":
            return ManagerIdentity(
                manager="systemd-user",
                target=self.target.manager_target,
                state="absent",
                instance_id=None,
                definition_path=None,
                runtime_pid=None,
            )
        if completed.returncode or fields.get("Id") != self.target.manager_target:
            raise ObservationUnavailable("systemd-user target is ambiguous")
        fragment = fields.get("FragmentPath", "")
        if self.target.definition_path is not None and _resolved(Path(fragment)) != _resolved(
            self.target.definition_path
        ):
            raise ObservationUnavailable("systemd-user fragment path differs from bound definition")
        raw_pid = fields.get("MainPID", "")
        runtime_pid = int(raw_pid) if raw_pid.isdigit() and int(raw_pid) > 0 else None
        invocation = fields.get("InvocationID", "") or None
        if runtime_pid is not None and invocation is None:
            raise ObservationUnavailable("systemd-user invocation identity is unavailable")
        return ManagerIdentity(
            manager="systemd-user",
            target=self.target.manager_target,
            state="present",
            instance_id=invocation,
            definition_path=fragment or None,
            runtime_pid=runtime_pid,
        )

    @staticmethod
    def _tree(root_pid: int, table: Mapping[int, tuple[int, str]]) -> tuple[int, ...]:
        children: dict[int, list[int]] = {}
        for pid, (parent_pid, _birth) in table.items():
            children.setdefault(parent_pid, []).append(pid)
        seen: set[int] = set()
        pending = [root_pid]
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            pending.extend(children.get(pid, ()))
        return tuple(sorted(seen))

    def _permitted_outside(
        self, table: Mapping[int, tuple[int, str]], excluded: set[int]
    ) -> tuple[ProcessIdentity, ...]:
        rows: list[ProcessIdentity] = []
        for pid in sorted(table):
            if pid in excluded:
                continue
            executable = self._exe(pid)
            if any(
                _same_path(executable, permitted) for permitted in self.target.allowed_executables
            ):
                rows.append(self._process_identity(pid, table))
        return tuple(rows)

    def capture(self) -> ProcessSnapshot:
        manager = self._manager()
        table = self._table()
        if manager.runtime_pid is None:
            return ProcessSnapshot(
                source=self.source_name,
                manager=manager,
                runtime=None,
                descendants=(),
                unbound_processes=self._permitted_outside(table, set()),
            )
        if manager.runtime_pid not in table:
            raise ObservationUnavailable("systemd-user runtime PID is absent from /proc")
        process_ids = self._tree(manager.runtime_pid, table)
        if len(process_ids) > self.max_processes:
            raise ObservationUnavailable(
                "manager descendant set exceeds bounded observation capacity"
            )
        identities = tuple(self._process_identity(pid, table) for pid in process_ids)
        runtime = next(identity for identity in identities if identity.pid == manager.runtime_pid)
        descendants = tuple(identity for identity in identities if identity.pid != runtime.pid)
        return ProcessSnapshot(
            source=self.source_name,
            manager=manager,
            runtime=runtime,
            descendants=descendants,
            unbound_processes=self._permitted_outside(table, set(process_ids)),
        )


class _MacSnapshotSource(_NativeSnapshotSource):
    """Direct launchd/``libproc`` source. It remains polling-only and non-authoritative."""

    source_name = "macos-bounded-launchd-libproc-poll"

    @staticmethod
    def _run(argv: list[str]) -> str:
        try:
            completed = subprocess.run(
                argv, capture_output=True, check=False, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ObservationUnavailable("macOS native command is unavailable") from exc
        if completed.returncode:
            text = (completed.stdout + completed.stderr).lower()
            if "could not find service" in text or "not found" in text:
                return ""
            raise ObservationUnavailable("macOS native command failed")
        return completed.stdout

    @staticmethod
    def _pid_path(pid: int) -> Path:
        try:
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            buffer = ctypes.create_string_buffer(4096)
            length = library.proc_pidpath(pid, buffer, len(buffer))
            if length <= 0:
                raise OSError(ctypes.get_errno(), "proc_pidpath failed")
            return Path(os.fsdecode(buffer.value)).resolve(strict=True)
        except OSError as exc:
            raise ObservationUnavailable("macOS process executable is unavailable") from exc

    def capture(self) -> ProcessSnapshot:
        # ``launchctl print`` is directly observed; parser uncertainty refuses rather
        # than accepting a partial source.  A macOS implementation can be promoted
        # only with a reviewed event source, not from this polling snapshot.
        output = self._run(["launchctl", "print", self.target.manager_target])
        if not output:
            raise ObservationUnavailable(
                "macOS polling source lacks a complete independently observed owner census"
            )
        pid_match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", output, re.MULTILINE)
        path_match = re.search(r"^\s*path\s*=\s*(.+?)\s*$", output, re.MULTILINE)
        if pid_match is None:
            raise ObservationUnavailable("launchd target has no exact runtime PID")
        definition = path_match.group(1).strip() if path_match else None
        if self.target.definition_path is not None and (
            definition is None
            or _resolved(Path(definition)) != _resolved(self.target.definition_path)
        ):
            raise ObservationUnavailable("launchd definition path differs from bound definition")
        pid = int(pid_match.group(1))
        # We intentionally do not use a caller's PID/start value. ``ps`` owns this
        # bounded birth observation.  No attempt is made to call it gap-free.
        output = self._run(["ps", "-p", str(pid), "-o", "lstart="])
        birth = output.strip()
        if not birth:
            raise ObservationUnavailable("macOS process birth observation is unavailable")
        self._identity(
            pid=pid,
            birth_id=birth,
            executable=self._pid_path(pid),
            parent_pid=None,
            parent_birth_id=None,
            discovery_id=f"macos-proc-v1:{pid}:{birth}",
        )
        _ = ManagerIdentity(
            manager="launchd",
            target=self.target.manager_target,
            state="present",
            instance_id=f"{self.target.manager_target}:{pid}:{birth}",
            definition_path=definition,
            runtime_pid=pid,
        )
        # A descendant census is not claimed from a partial ps parse.  Returning a
        # clear unavailability is safer than silently omitting children.
        raise ObservationUnavailable(
            "macOS polling source lacks a complete independently observed descendant census"
        )


class _WindowsSnapshotSource(_NativeSnapshotSource):
    """Independent Task Scheduler/CIM polling source; it does not use Q's CIM parser."""

    source_name = "windows-bounded-task-cim-poll"

    def _powershell(self) -> Path:
        path = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        if not path.is_file():
            raise ObservationUnavailable("fixed Windows PowerShell executable is unavailable")
        return path

    def capture(self) -> ProcessSnapshot:
        escaped_target = self.target.manager_target.replace("'", "''")
        script = (
            "$ErrorActionPreference='Stop';"
            "$svc=New-Object -ComObject 'Schedule.Service';$svc.Connect();"
            "$task=$null;try{$task=$svc.GetTask('" + escaped_target + "')}"
            "catch [System.Runtime.InteropServices.COMException]"
            "{if($_.Exception.HResult -ne -2147024894){throw}};"
            "$instances=@();if($null -ne $task){$instances=@($task.GetInstances(0)|ForEach-Object"
            "{[pscustomobject]@{Pid=$_.EnginePID;Instance=[string]$_.InstanceGuid}})};"
            "$rows=@(Get-CimInstance Win32_Process|ForEach-Object{[pscustomobject]@{"
            "Pid=[int]$_.ProcessId;Parent=[int]$_.ParentProcessId;Birth=[string]$_.CreationDate;"
            "Exe=[string]$_.ExecutablePath}});"
            "[pscustomobject]@{Present=($null -ne $task);Instances=$instances;Processes=$rows}|"
            "ConvertTo-Json -Depth 5 -Compress"
        )
        try:
            completed = subprocess.run(
                [str(self._powershell()), "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                check=False,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ObservationUnavailable("Task Scheduler/CIM observation is unavailable") from exc
        if completed.returncode:
            raise ObservationUnavailable("Task Scheduler/CIM observation failed")
        try:
            row = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ObservationUnavailable("Task Scheduler/CIM observation is malformed") from exc
        if not isinstance(row, dict) or not isinstance(row.get("Present"), bool):
            raise ObservationUnavailable("Task Scheduler/CIM observation is ambiguous")
        if not row["Present"]:
            raise ObservationUnavailable(
                "Windows polling source lacks a complete independently observed owner census"
            )
        # A full current-user task + descendant census requires additional reviewed
        # privilege and controller semantics.  Do not turn a partial CIM response
        # into an apparent direct-platform observation.
        raise ObservationUnavailable(
            "Windows polling source lacks a complete independently observed task descendant census"
        )


def _source_for(
    target: NativeServiceObservationTarget, max_processes: int
) -> _NativeSnapshotSource:
    sources: dict[str, type[_NativeSnapshotSource]] = {
        "linux": _LinuxSnapshotSource,
        "macos": _MacSnapshotSource,
        "windows": _WindowsSnapshotSource,
    }
    return sources[target.os_name](target, max_processes)


class ProcessTransitionObserver:
    """Own a private bounded ledger of native manager/process observations.

    The only current backends are polling backends.  ``assert_authorizable``
    therefore *always* refuses after recording the source limitation.  This is
    intentional: snapshot differences are recorded as differences, never
    promoted into inferred start/stop/restart transitions.
    """

    def __init__(
        self,
        target: NativeServiceObservationTarget,
        *,
        max_events: int = 256,
        max_processes: int = 128,
    ) -> None:
        if type(max_processes) is not int or max_processes < 1:
            raise ValueError("max_processes must be positive")
        self._target = target
        self._source = _source_for(target, max_processes)
        self._ledger = _TransitionLedger(max_events)
        self._lock = threading.RLock()
        self._armed = False
        self._closed = False
        self._last_snapshot: ProcessSnapshot | None = None
        self._last_source_sequence: int | None = None
        self._refusal: str | None = None
        self._ledger.append(
            "observer-created",
            "unknown",
            {
                "source": self._source.source_name,
                "continuity": "not-proven",
                "restart": "new ledger cannot resume any prior observer",
            },
        )

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        with self._lock:
            return self._ledger.events

    @property
    def latest_snapshot(self) -> ProcessSnapshot | None:
        with self._lock:
            return self._last_snapshot

    def _refuse(self, reason: str, *, kind: str = "observation-gap") -> None:
        if self._refusal is None:
            self._refusal = reason
        self._ledger.append(kind, "gap", {"reason": reason, "retention": "refuse"})

    def _capture(self) -> ProcessSnapshot:
        try:
            snapshot = self._source.capture()
        except ObservationRefused as exc:
            self._refuse(str(exc), kind="source-unavailable")
            raise
        except Exception as exc:  # no foreign exception is permitted to look successful
            self._refuse(type(exc).__name__, kind="source-failure")
            raise ObservationUnavailable("OS source failed unexpectedly") from exc
        return snapshot

    @staticmethod
    def _summary(snapshot: ProcessSnapshot) -> dict[str, object]:
        return {
            "snapshot_sha256": snapshot.fingerprint,
            "manager": snapshot.manager.record(),
            "process_keys": [list(identity.key) for identity in snapshot.identities],
            "source": snapshot.source,
            "source_sequence": snapshot.source_sequence,
            "continuity": "not-proven",
        }

    def _record_sequence(self, snapshot: ProcessSnapshot) -> None:
        sequence = snapshot.source_sequence
        if sequence is None:
            if self._last_source_sequence is None:
                self._ledger.append(
                    "event-source-unavailable",
                    "unknown",
                    {
                        "source": snapshot.source,
                        "reason": "bounded polling cannot prove gap-free continuity",
                    },
                )
                if self._refusal is None:
                    self._refusal = "bounded polling cannot prove gap-free continuity"
            return
        if self._last_source_sequence is not None and sequence != self._last_source_sequence + 1:
            self._refuse(
                "event-source sequence discontinuity",
                kind="event-sequence-discontinuity",
            )
        self._last_source_sequence = sequence

    def arm_initial_absence(self) -> None:
        """Observe exact initial absence before a provider permits registration/start."""
        with self._lock:
            if self._armed or self._closed:
                raise ObservationRefused("observer is not available for initial absence")
            snapshot = self._capture()
            self._record_sequence(snapshot)
            if snapshot.manager.state != "absent" or snapshot.identities:
                self._refuse(
                    "bound native owner is not exactly absent", kind="initial-absence-failed"
                )
                raise ObservationRefused("bound native owner is not exactly absent")
            self._last_snapshot = snapshot
            self._armed = True
            self._ledger.append("initial-absence-snapshot", "observed", self._summary(snapshot))

    def poll(self) -> ProcessSnapshot:
        """Record a bounded snapshot difference without inferring a lifecycle transition."""
        with self._lock:
            if not self._armed or self._closed:
                raise ObservationRefused("observer is not armed")
            snapshot = self._capture()
            self._record_sequence(snapshot)
            previous = self._last_snapshot
            kind = "snapshot-repeat"
            if previous is None or previous.fingerprint != snapshot.fingerprint:
                kind = "snapshot-difference-unproven"
            self._last_snapshot = snapshot
            self._ledger.append(kind, "observed", self._summary(snapshot))
            return snapshot

    def assert_authorizable(self, phase: str) -> None:
        """Fail closed for authorization/finalization unless a future reviewed source changes this."""
        with self._lock:
            if phase not in {"authorization", "finalization"}:
                raise ValueError("unknown observation phase")
            if not self._armed or self._closed:
                raise ObservationRefused("observer is not live for this phase")
            if self._ledger.overflowed:
                raise ObservationRefused("transition ledger overflowed")
            if self._refusal is not None:
                raise ObservationRefused(f"transition ledger refused: {self._refusal}")
            raise ObservationRefused(
                f"{phase} refused: bounded polling has no proof of gap-free continuity"
            )

    def close(self, reason: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._ledger.append("observer-closed", "unknown", {"reason": reason})

    def report(self) -> dict[str, object]:
        with self._lock:
            return {
                "source": self._source.source_name,
                "armed": self._armed,
                "closed": self._closed,
                "event_count": len(self._ledger.events),
                "overflowed": self._ledger.overflowed,
                "authorization": "refused",
                "continuity": "not-proven-bounded-polling",
                "refusal": self._refusal or "bounded polling cannot prove gap-free continuity",
            }
