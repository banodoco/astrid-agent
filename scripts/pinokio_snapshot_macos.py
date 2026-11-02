#!/usr/bin/env python3
"""Bounded macOS process and launchd snapshot collector for Pinokio journeys.

The collector observes the operating system directly.  It is deliberately a
snapshot source: callers own action-boundary and polling cadence, receipt
bindings, and any proof rule applied to the returned observation.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence


SCHEMA = "astrid.r25.snapshot.macos.v1"
SOURCE_PROFILE = "astrid.r25.macos-15.bash.ps-libproc-launchctl.v1"

DEFAULT_MAX_PROCESSES = 4096
DEFAULT_MAX_PS_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_SERVICE_TARGETS = 64
DEFAULT_MAX_SERVICE_BYTES = 256 * 1024
DEFAULT_COMMAND_TIMEOUT_SECONDS = 10

_PS = "/bin/ps"
_LAUNCHCTL = "/bin/launchctl"
_LIBPROC = "/usr/lib/libproc.dylib"
_PS_ARGS = ("-axo", "pid=,ppid=,lstart=,command=")
_PS_ROW = re.compile(
    r"^\s*(?P<pid>[0-9]+)\s+(?P<ppid>[0-9]+)\s+"
    r"(?P<weekday>[A-Z][a-z]{2})\s+(?P<month>[A-Z][a-z]{2})\s+"
    r"(?P<day>[0-9]{1,2})\s+(?P<clock>[0-9]{2}:[0-9]{2}:[0-9]{2})\s+"
    r"(?P<year>[0-9]{4})\s+(?P<command>.+)$"
)
_TARGET = re.compile(
    r"^(?:system|(?:gui|user|pid)/[0-9]{1,20})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9_-])?$"
)
_ABSENT = re.compile(
    r"(?:could\s+not\s+find\s+service|service[^\n]{0,160}not\s+found|"
    r"no\s+such\s+(?:process|service))",
    re.IGNORECASE,
)


class CollectorError(RuntimeError):
    """A required source was ambiguous, malformed, denied, or over capacity."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class Limits:
    max_processes: int = DEFAULT_MAX_PROCESSES
    max_ps_bytes: int = DEFAULT_MAX_PS_BYTES
    max_service_targets: int = DEFAULT_MAX_SERVICE_TARGETS
    max_service_bytes: int = DEFAULT_MAX_SERVICE_BYTES
    command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        for field_name in (
            "max_processes",
            "max_ps_bytes",
            "max_service_targets",
            "max_service_bytes",
            "command_timeout_seconds",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")

    def record(self) -> dict[str, int]:
        return {
            "command_timeout_seconds": self.command_timeout_seconds,
            "max_processes": self.max_processes,
            "max_ps_bytes": self.max_ps_bytes,
            "max_service_bytes": self.max_service_bytes,
            "max_service_targets": self.max_service_targets,
        }


CommandRunner = Callable[[Sequence[str], int], CommandResult]
PidPathResolver = Callable[[int], str]


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CollectorError("observation is not canonical-JSON-compatible") from exc


def structural_digest(observation: dict[str, object]) -> str:
    unsigned = dict(observation)
    unsigned.pop("structural_sha256", None)
    return hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()


def _default_runner(argv: Sequence[str], timeout_seconds: int) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectorError(f"native command timed out: {argv[0]}") from exc
    except OSError as exc:
        raise CollectorError(f"native command unavailable: {argv[0]}") from exc
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class _LibprocPidPath:
    def __init__(self) -> None:
        try:
            self._library = ctypes.CDLL(_LIBPROC, use_errno=True)
        except OSError as exc:
            raise CollectorError("libproc is unavailable") from exc
        self._proc_pidpath = self._library.proc_pidpath
        self._proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self._proc_pidpath.restype = ctypes.c_int

    def __call__(self, pid: int) -> str:
        buffer = ctypes.create_string_buffer(4096)
        ctypes.set_errno(0)
        length = self._proc_pidpath(pid, buffer, len(buffer))
        if length <= 0:
            error = ctypes.get_errno()
            if error in {errno.ESRCH, errno.ENOENT}:
                raise ProcessLookupError(error, "process vanished")
            if error in {errno.EPERM, errno.EACCES}:
                raise PermissionError(error, "proc_pidpath denied")
            raise OSError(error or errno.EIO, "proc_pidpath failed")
        raw = bytes(buffer[:length]).split(b"\0", 1)[0]
        try:
            path = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CollectorError("proc_pidpath returned a non-UTF-8 path") from exc
        if not path or "\0" in path or not os.path.isabs(path):
            raise CollectorError("proc_pidpath returned an unsafe executable path")
        return path


def _bounded_text(data: object, *, maximum: int, source: str) -> str:
    if not isinstance(data, (bytes, bytearray)):
        raise CollectorError(f"{source} returned non-byte output")
    raw = bytes(data)
    if len(raw) > maximum:
        raise CollectorError(f"{source} output exceeds bounded capacity")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CollectorError(f"{source} output is not UTF-8") from exc
    if "\0" in text:
        raise CollectorError(f"{source} output contains NUL")
    return text


def _command(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    timeout_seconds: int,
    maximum: int,
    source: str,
) -> tuple[int, str, str]:
    result = runner(tuple(argv), timeout_seconds)
    if not isinstance(result, CommandResult) or type(result.returncode) is not int:
        raise CollectorError(f"{source} command seam returned a malformed result")
    stdout = _bounded_text(result.stdout, maximum=maximum, source=f"{source} stdout")
    stderr = _bounded_text(result.stderr, maximum=maximum, source=f"{source} stderr")
    if len(result.stdout) + len(result.stderr) > maximum:
        raise CollectorError(f"{source} combined output exceeds bounded capacity")
    return result.returncode, stdout, stderr


def _parse_processes(
    text: str,
    *,
    maximum: int,
    pid_path: PidPathResolver,
) -> list[dict[str, object]]:
    if not text or not text.endswith("\n"):
        raise CollectorError("ps output is empty or unterminated")
    rows: list[dict[str, object]] = []
    seen: set[int] = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise CollectorError(f"ps row {line_number} is empty")
        match = _PS_ROW.fullmatch(line)
        if match is None:
            raise CollectorError(f"ps row {line_number} is malformed")
        pid = int(match.group("pid"))
        parent_pid = int(match.group("ppid"))
        if pid <= 0 or parent_pid < 0:
            raise CollectorError(f"ps row {line_number} has an invalid PID")
        if pid in seen:
            raise CollectorError(f"ps output contains duplicate PID {pid}")
        seen.add(pid)
        if len(rows) >= maximum:
            raise CollectorError("process table exceeds bounded capacity")
        birth = (
            f"{match.group('weekday')} {match.group('month')} "
            f"{int(match.group('day')):02d} {match.group('clock')} {match.group('year')}"
        )
        try:
            dt.datetime.strptime(birth, "%a %b %d %H:%M:%S %Y")
        except ValueError as exc:
            raise CollectorError(f"ps row {line_number} has an invalid birth time") from exc
        command_line = match.group("command")
        if not command_line or "\0" in command_line:
            raise CollectorError(f"ps row {line_number} has an invalid command line")
        state = "present"
        executable: str | None
        try:
            executable = pid_path(pid)
        except ProcessLookupError:
            state = "vanished"
            executable = None
        except PermissionError as exc:
            raise CollectorError(f"executable path permission denied for PID {pid}") from exc
        except (OSError, UnicodeError) as exc:
            raise CollectorError(f"executable path unavailable for stable PID {pid}") from exc
        if state == "present" and (
            not isinstance(executable, str)
            or not executable
            or "\0" in executable
            or not os.path.isabs(executable)
        ):
            raise CollectorError(f"executable path is malformed for PID {pid}")
        rows.append(
            {
                "birth_text": birth,
                "collection_state": state,
                "command_line": command_line,
                "executable_path": executable,
                "parent_pid": parent_pid,
                "pid": pid,
            }
        )
    if not rows:
        raise CollectorError("ps returned no process rows")
    rows.sort(key=lambda row: int(row["pid"]))
    return rows


def _validated_targets(targets: Iterable[str], maximum: int) -> list[str]:
    if isinstance(targets, (str, bytes)):
        raise CollectorError("launchd targets must be a sequence")
    result: list[str] = []
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, str) or _TARGET.fullmatch(target) is None:
            raise CollectorError("launchd target is unsafe or malformed")
        if target in seen:
            raise CollectorError(f"duplicate launchd target: {target}")
        seen.add(target)
        result.append(target)
        if len(result) > maximum:
            raise CollectorError("launchd target set exceeds bounded capacity")
    result.sort()
    return result


def _service_rows(
    targets: Sequence[str], runner: CommandRunner, limits: Limits
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for target in targets:
        try:
            returncode, stdout, stderr = _command(
                runner,
                (_LAUNCHCTL, "print", target),
                timeout_seconds=limits.command_timeout_seconds,
                maximum=limits.max_service_bytes,
                source=f"launchctl print {target}",
            )
        except CollectorError as exc:
            rows.append(
                {
                    "error": str(exc),
                    "raw_print": None,
                    "returncode": None,
                    "state": "error",
                    "target": target,
                }
            )
            continue
        combined = stdout + stderr
        if returncode == 0:
            if not stdout.strip():
                rows.append(
                    {
                        "error": "launchctl returned empty successful output",
                        "raw_print": None,
                        "returncode": 0,
                        "state": "error",
                        "target": target,
                    }
                )
            else:
                rows.append(
                    {
                        "error": None,
                        "raw_print": stdout,
                        "returncode": 0,
                        "state": "present",
                        "target": target,
                    }
                )
        elif _ABSENT.search(combined):
            rows.append(
                {
                    "error": "not-found",
                    "raw_print": None,
                    "returncode": returncode,
                    "state": "absent",
                    "target": target,
                }
            )
        else:
            rows.append(
                {
                    "error": f"launchctl exited {returncode}",
                    "raw_print": None,
                    "returncode": returncode,
                    "state": "error",
                    "target": target,
                }
            )
    return rows


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def collect_snapshot(
    launchd_targets: Iterable[str],
    *,
    limits: Limits | None = None,
    runner: CommandRunner = _default_runner,
    pid_path_resolver: PidPathResolver | None = None,
    utc_now: Callable[[], str] = _utc_now,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    collector_pid: int | None = None,
) -> dict[str, object]:
    """Capture one fail-closed, canonical-JSON-compatible macOS snapshot."""
    active_limits = limits or Limits()
    targets = _validated_targets(launchd_targets, active_limits.max_service_targets)
    started = monotonic_ns()
    if type(started) is not int or started < 0:
        raise CollectorError("monotonic clock returned an invalid start value")
    returncode, stdout, _stderr = _command(
        runner,
        (_PS, *_PS_ARGS),
        timeout_seconds=active_limits.command_timeout_seconds,
        maximum=active_limits.max_ps_bytes,
        source="ps",
    )
    if returncode != 0:
        raise CollectorError(f"ps failed with exit {returncode}")
    resolver = pid_path_resolver or _LibprocPidPath()
    processes = _parse_processes(
        stdout, maximum=active_limits.max_processes, pid_path=resolver
    )
    launchd = _service_rows(targets, runner, active_limits)
    finished = monotonic_ns()
    if type(finished) is not int or finished < started:
        raise CollectorError("monotonic clock moved backwards")
    pid = os.getpid() if collector_pid is None else collector_pid
    if type(pid) is not int or pid <= 0:
        raise CollectorError("collector PID must be positive")
    captured_at = utc_now()
    if not isinstance(captured_at, str) or not captured_at.endswith("Z"):
        raise CollectorError("UTC clock returned a malformed timestamp")
    observation: dict[str, object] = {
        "capture_finished_monotonic_ns": str(finished),
        "capture_started_monotonic_ns": str(started),
        "captured_at_utc": captured_at,
        "collector_pid": pid,
        "launchd": launchd,
        "limits": active_limits.record(),
        "processes": processes,
        "schema": SCHEMA,
        "source_profile": SOURCE_PROFILE,
    }
    observation["structural_sha256"] = structural_digest(observation)
    canonical_json_bytes(observation)
    return observation


capture_snapshot = collect_snapshot


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launchd-target", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        observation = collect_snapshot(args.launchd_target)
    except (CollectorError, ValueError) as exc:
        print(f"macOS snapshot refused: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_json_bytes(observation) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
