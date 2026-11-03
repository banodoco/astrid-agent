"""Per-call launch of one selected human ComfyUI; no discovery or fallback.

``prepare_target_launch`` retains local path evidence for a caller to review.
``launch_target`` requires fresh ``user_env`` consent even with a retained plan.
Neither saves facts, installs anything, nor acquires control of a human process.
The returned readiness uses the existing preflight envelope. A timeout leaves
the process alone and returns manual-start guidance, never inferred readiness.

Direct Python/main.py launches are supported. Desktop/Pinokio wrappers without
an independently observable exact child command require manual start. The
default observer uses Linux procfs without helper processes or Runtime imports;
other platforms can supply the same read-only observer interface or fail closed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .backends.comfy_target import ComfyTarget, ComfyTargetClient, validate_attached_url
from .preflight import readiness_envelope
from .targets import TargetRecord, TargetSelection


class LaunchVerificationError(ValueError):
    """Current facts cannot authorize this exact launch."""


@dataclass(frozen=True)
class PathIdentity:
    path: str
    resolved: str
    # Include both lexical and resolved ancestors, including symlink inodes.
    components: tuple[tuple, ...]
    digest: str | None


def _path_identity(value: str, *, directory: bool = False) -> PathIdentity:
    if (not isinstance(value, str) or not value or not Path(value).is_absolute()
            or any(ord(c) < 32 for c in value) or ".." in Path(value).parts):
        raise LaunchVerificationError("launch paths must be explicit absolute paths")
    path = Path(value)
    resolved = path.resolve(strict=True)
    components = []
    pending = [*reversed(path.parents), path, *reversed(resolved.parents), resolved]
    visited = set()
    for candidate in pending:
        if candidate in visited:
            continue
        visited.add(candidate)
        if len(visited) > 1024:
            raise LaunchVerificationError("launch symlink chain exceeds its bound")
        info = candidate.lstat()
        link = os.readlink(candidate) if stat.S_ISLNK(info.st_mode) else None
        components.append((str(candidate), info.st_dev, info.st_ino, info.st_mode,
                           link, info.st_ctime_ns if link is not None else None))
        if link is not None:
            destination = Path(os.path.abspath(candidate.parent / link))
            pending.extend((*reversed(destination.parents), destination))
    before = path.stat()
    if directory:
        if not stat.S_ISDIR(before.st_mode):
            raise LaunchVerificationError("selected root is not a directory")
        digest = None
    else:
        if not stat.S_ISREG(before.st_mode):
            raise LaunchVerificationError("selected launch file is not a regular file")
        if before.st_size > 256 * 1024 * 1024:
            raise LaunchVerificationError("selected launch file exceeds its verification bound")
        # Stream the interpreter too; do not trust only a filename or timestamp.
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
                if size > 256 * 1024 * 1024:
                    raise LaunchVerificationError("selected launch file grew beyond its verification bound")
                hasher.update(block)
        digest = hasher.hexdigest()
        after = path.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise LaunchVerificationError("launch file changed during verification")
    return PathIdentity(str(path), str(resolved), tuple(components), digest)


@dataclass(frozen=True)
class ProcessObservation:
    pid: int
    birth_id: str
    argv: tuple[str, ...]
    root: str
    executable: str


class LaunchObserver(Protocol):
    """Host observations, independent of HTTP and the saved target facts.

    Implementations must bound work by deadline and raise if evidence is
    unavailable. An empty listener tuple must prove that the port is unused.
    """

    def process(self, pid: int, *, deadline: float) -> ProcessObservation | None: ...
    def listeners(self, endpoint: str, *, deadline: float) -> tuple[ProcessObservation, ...]: ...


class ProcLaunchObserver:
    """Read-only Linux evidence, including kernel birth and socket ownership."""

    @staticmethod
    def _check(deadline):
        if not sys.platform.startswith("linux"):
            raise LaunchVerificationError("native process observation unavailable")
        if time.monotonic() >= deadline:
            raise TimeoutError("native process observation deadline expired")

    @staticmethod
    def _read(path, limit=65536):
        with Path(path).open("rb") as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise LaunchVerificationError("native observation exceeds its bound")
        return value

    def _birth(self, folder):
        fields = self._read(folder / "stat").decode().rsplit(")", 1)[1].split()
        if fields[0] in {"Z", "X"}:
            return None
        boot = self._read("/proc/sys/kernel/random/boot_id").decode().strip()
        if not boot or not fields[19].isdigit():
            raise LaunchVerificationError("process birth is unknown")
        return boot + ":" + fields[19]

    def process(self, pid, *, deadline):
        self._check(deadline)
        if type(pid) is not int or pid < 1:
            raise LaunchVerificationError("invalid process pid")
        folder = Path("/proc") / str(pid)
        try:
            before = self._birth(folder)
            if before is None:
                return None
            argv = tuple(os.fsdecode(v) for v in self._read(folder / "cmdline").rstrip(b"\0").split(b"\0"))
            result = ProcessObservation(pid, before, argv,
                                        os.readlink(folder / "cwd"), os.readlink(folder / "exe"))
            if self._birth(folder) != before:
                raise LaunchVerificationError("process birth changed during observation")
            self._check(deadline)
            return result
        except FileNotFoundError:
            # Disappearance of cwd/exe alone is not proof of process absence.
            if not folder.exists():
                return None
            raise LaunchVerificationError("process evidence disappeared") from None

    def listeners(self, endpoint, *, deadline):
        self._check(deadline)
        port = int(endpoint.rsplit(":", 1)[1])
        inodes = set()
        for name in ("tcp", "tcp6"):
            table = Path("/proc/net") / name
            try:
                rows = self._read(table, 4 * 1024 * 1024).decode().splitlines()[1:]
            except FileNotFoundError:
                if name == "tcp6":
                    continue
                raise
            for row in rows:
                fields = row.split()
                if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                    inodes.add(fields[9])
        if not inodes:
            self._check(deadline)
            return ()
        found, owners = set(), []
        # Unknown/inaccessible owners never become an empty-port observation.
        for index, folder in enumerate(Path("/proc").iterdir()):
            self._check(deadline)
            if index > 65536:
                raise LaunchVerificationError("process scan exceeds its bound")
            if not folder.name.isdigit():
                continue
            try:
                before = self.process(int(folder.name), deadline=deadline)
                if before is None:
                    continue
                owned = set()
                for fd_index, fd in enumerate((folder / "fd").iterdir()):
                    self._check(deadline)
                    if fd_index > 65536:
                        raise LaunchVerificationError("descriptor scan exceeds its bound")
                    try:
                        link = os.readlink(fd)
                    except FileNotFoundError:
                        continue
                    if link.startswith("socket:[") and link[8:-1] in inodes:
                        owned.add(link[8:-1])
                if owned:
                    after = self.process(before.pid, deadline=deadline)
                    if after != before:
                        raise LaunchVerificationError("listener process changed during observation")
                    found.update(owned)
                    owners.append(before)
            except (FileNotFoundError, PermissionError):
                continue
        if found != inodes or not owners:
            raise LaunchVerificationError("listener ownership is unknown")
        return tuple(owners)


@dataclass(frozen=True)
class TargetLaunchPlan:
    target_json: str
    endpoint: str
    root: str
    python: str
    app: str
    argv: tuple[str, ...]
    paths: tuple[PathIdentity, ...]
    previous_process: tuple[int, str] | None


@dataclass(frozen=True)
class TargetLaunchResult:
    target: TargetRecord | None
    readiness: dict
    launched: bool = False
    pid: int | None = None
    process_birth_id: str | None = None

    @property
    def ready(self) -> bool:
        return self.readiness["status"] in {"ready", "ready_with_warnings"}


def _selected(target):
    record = target.record if isinstance(target, TargetSelection) else target
    if not isinstance(record, TargetRecord):
        raise LaunchVerificationError("select one existing human ComfyUI installation")
    if (record.engine, record.location, record.owner) != ("comfyui", "local", "human"):
        raise LaunchVerificationError("launch requires the selected local human ComfyUI")
    return record


def _target_json(record):
    return json.dumps(record.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)


def prepare_target_launch(target: TargetRecord | TargetSelection, *,
                          endpoint_override: str | None = None) -> TargetLaunchPlan:
    """Retain exact launch facts and path evidence; perform no probe or spawn.

    Facts use ``launch_app`` (the selected root's main.py), ``launch_argv``
    (full argv, starting with the selected Python), and optional
    ``launch_process={pid, birth_id}`` from a prior native observation.
    Legacy HTTP ``argv`` and guessed Python paths are insufficient authority.
    An explicit endpoint override always requires a new independent selection.
    """
    record = _selected(target)
    if endpoint_override is not None:
        raise LaunchVerificationError("endpoint override invalidates prior launch facts; select and verify it anew")
    if not record.endpoint or not record.root or not record.python:
        raise LaunchVerificationError("selected endpoint, root and Python must be verified before launch")
    endpoint = validate_attached_url(record.endpoint)
    app, argv = record.facts.get("launch_app"), record.facts.get("launch_argv")
    if (not isinstance(argv, (list, tuple)) or len(argv) < 2
            or any(not isinstance(v, str) or not v or any(ord(c) < 32 for c in v) for v in argv)):
        raise LaunchVerificationError("launch requires exact argv tokens")
    if argv[0] != record.python:
        raise LaunchVerificationError("argv must use the exact selected Python path")
    # Never reinterpret a shell command, Python -c/-m, or launcher script.
    script_index = 1
    while script_index < len(argv) and argv[script_index] in {"-s", "-E", "-I", "-u", "-B"}:
        script_index += 1
    if (not isinstance(app, str) or app != str(Path(record.root) / "main.py")
            or script_index >= len(argv) or argv[script_index] not in {app, "main.py", "./main.py"}):
        raise LaunchVerificationError("launch app must be the selected root's explicit main.py")
    ports = []
    for index, arg in enumerate(argv[script_index + 1:], script_index + 1):
        if arg == "--port":
            ports.append(argv[index + 1] if index + 1 < len(argv) else "")
        elif arg.startswith("--port="):
            ports.append(arg.partition("=")[2])
    if (len(ports) > 1 or (ports and not ports[0].isascii())
            or (ports and not ports[0].isdigit())
            or int(ports[0] if ports else "8188") != int(endpoint.rsplit(":", 1)[1])):
        raise LaunchVerificationError("launch argv port does not match the selected endpoint")
    paths = (_path_identity(record.root, directory=True),
             _path_identity(record.python), _path_identity(app))
    if not os.access(record.python, os.X_OK):
        raise LaunchVerificationError("selected Python is not executable")
    # Bind the selected environment configuration as well as a symlinked binary.
    venv_config = Path(record.python).parent.parent / "pyvenv.cfg"
    if venv_config.exists() or venv_config.is_symlink():
        paths += (_path_identity(str(venv_config)),)
    previous = record.facts.get("launch_process")
    if previous is not None:
        if (not isinstance(previous, dict) or set(previous) != {"pid", "birth_id"}
                or type(previous["pid"]) is not int or previous["pid"] < 1
                or not isinstance(previous["birth_id"], str) or not previous["birth_id"]):
            raise LaunchVerificationError("recorded process birth is invalid")
        previous = (previous["pid"], previous["birth_id"])
    return TargetLaunchPlan(_target_json(record), endpoint, record.root, record.python,
                            app, tuple(argv), paths, previous)


def _revalidate(plan, record):
    current = prepare_target_launch(record)
    if current != plan:
        raise LaunchVerificationError("selected launch facts or path identity changed")


def _match_process(plan, process):
    if (not isinstance(process, ProcessObservation) or type(process.pid) is not int
            or process.pid < 1 or not process.birth_id or process.argv != plan.argv
            or process.root != plan.paths[0].resolved
            or process.executable != plan.paths[1].resolved):
        raise LaunchVerificationError("native process does not match selected app, argv, root and Python")
    return process


def _health_probe(endpoint, *, timeout):
    payload = ComfyTargetClient(ComfyTarget(endpoint, "attached")).request_json(
        "/system_stats", timeout=timeout, response_limit=64 * 1024)
    return (isinstance(payload, dict) and isinstance(payload.get("system"), dict)
            and isinstance(payload.get("devices"), list))


def launch_target(target: TargetRecord | TargetSelection, *, consent=(),
                  plan: TargetLaunchPlan | None = None, endpoint_override: str | None = None,
                  timeout: float = 10.0, probe_timeout: float = 1.0, poll_interval: float = 0.1,
                  observer: LaunchObserver | None = None, spawn=None, health_probe=None,
                  monotonic=None, sleep=None) -> TargetLaunchResult:
    """Launch/re-probe only this target and return the ordinary readiness shape.

    Injected observer/probe functions must honor their deadline/timeout. Spawn
    receives only exact argv and Popen keyword arguments, with ``shell=False``.
    At most one spawn and 100 probes occur. A launched process is never killed,
    restarted, or adopted as an Astrid-managed process, even on timeout.
    """
    clock, pause = monotonic or time.monotonic, sleep or time.sleep
    record, launched, process = None, False, None

    def blocked(reason, message, *, code="target_unreachable"):
        details = {"reason": reason, "manual_start": True,
                   "process_may_be_running": launched or process is not None}
        fixes = []
        if reason == "launch_timeout" and isinstance(plan, TargetLaunchPlan):
            fixes.append({"argv": list(plan.argv), "scope": "user_env", "details": {
                "manual_start": True, "cwd": plan.root, "check_existing_process": True}})
        elif record is not None:
            fixes.append({"argv": ["astrid", "generate", "targets", "--target",
                                    record.endpoint or "auto"], "scope": "read_only",
                          "details": {"manual_start": True}})
        envelope = readiness_envelope(target=record, issues=[{
            "code": code, "severity": "error", "message": message, "details": details,
        }], fixes=fixes)
        return TargetLaunchResult(record, envelope, launched,
                                  process.pid if process else None,
                                  process.birth_id if process else None)

    try:
        record = _selected(target)
        if endpoint_override is not None:
            # No old paths or ownership are projected onto the override result,
            # including calls which have not supplied launch consent.
            record = TargetRecord(endpoint=validate_attached_url(endpoint_override))
            return blocked("endpoint_override", "Endpoint override invalidates prior launch facts; verify the new target before launch")
        # Saved preferences/facts and a retained plan never supply consent.
        if not isinstance(consent, (tuple, list, set, frozenset)) or "user_env" not in consent:
            return blocked("user_env_required", "Launching the selected human ComfyUI requires per-call user_env consent",
                           code="input_rejected")
        for value in (timeout, probe_timeout, poll_interval):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 30:
                return blocked("invalid_bound", "Launch bounds must be finite, positive and at most 30 seconds", code="input_rejected")
        deadline = clock() + timeout
        observer = observer or ProcLaunchObserver()
        health_probe = health_probe or _health_probe
        spawn = spawn or subprocess.Popen
        plan = plan or prepare_target_launch(record)
        _revalidate(plan, record)
        if plan.previous_process:
            pid, birth = plan.previous_process
            process = observer.process(pid, deadline=deadline)
            if process is not None:
                if process.birth_id != birth:
                    raise LaunchVerificationError("recorded process PID was reused")
                _match_process(plan, process)
        listeners = observer.listeners(plan.endpoint, deadline=deadline)
        if listeners:
            if len(listeners) != 1:
                raise LaunchVerificationError("selected port has ambiguous listeners")
            listener = _match_process(plan, listeners[0])
            if process is not None and listener != process:
                raise LaunchVerificationError("listener differs from recorded process birth")
            process = listener
        if process is None:
            # Independently reread paths/selection and the port at the last
            # pre-spawn boundary. Never spawn over another listener.
            _revalidate(plan, record)
            if observer.listeners(plan.endpoint, deadline=deadline):
                raise LaunchVerificationError("listener appeared before spawn")
            _revalidate(plan, record)
            if clock() >= deadline:
                return blocked("launch_timeout", "Launch verification timed out; inspect and start the selected ComfyUI manually")
            child = spawn(list(plan.argv), cwd=plan.root, shell=False, close_fds=True,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, start_new_session=True)
            launched = True
            process = _match_process(plan, observer.process(child.pid, deadline=deadline))
            if process.pid != child.pid:
                raise LaunchVerificationError("spawn returned a different process identity")
        # Pin birth before any HTTP. Every health result is bracketed by fresh
        # path, native-process and listener observations, independent of HTTP.
        for _ in range(100):
            if clock() >= deadline:
                break
            _revalidate(plan, record)
            if observer.process(process.pid, deadline=deadline) != process:
                raise LaunchVerificationError("selected process birth or command changed")
            listeners = observer.listeners(plan.endpoint, deadline=deadline)
            if listeners and listeners != (process,):
                raise LaunchVerificationError("another process is listening on the selected endpoint")
            healthy = False
            remaining = deadline - clock()
            if listeners and remaining > 0:
                try:
                    healthy = health_probe(plan.endpoint, timeout=min(probe_timeout, remaining)) is True
                except (OSError, ValueError):
                    pass
            if healthy:
                _revalidate(plan, record)
                if (observer.process(process.pid, deadline=deadline) != process
                        or observer.listeners(plan.endpoint, deadline=deadline) != (process,)):
                    raise LaunchVerificationError("process/listener identity changed during health probe")
                if clock() >= deadline:
                    break
                fresh = replace(record, facts={**record.facts, "launch_observation": {
                    "endpoint": plan.endpoint, "pid": process.pid, "birth_id": process.birth_id}})
                return TargetLaunchResult(fresh, readiness_envelope(target=fresh),
                                          launched, process.pid, process.birth_id)
            pause(min(poll_interval, max(0.0, deadline - clock())))
        return blocked("launch_timeout", "Selected ComfyUI did not become ready in time; inspect the existing process and start it manually if needed")
    except TimeoutError:
        return blocked("launch_timeout", "Selected ComfyUI verification timed out; inspect the existing process and start it manually if needed")
    except (OSError, ValueError, RuntimeError, IndexError, TypeError) as error:
        return blocked("launch_unverified", f"Selected ComfyUI launch could not be verified: {error}; start it manually")
