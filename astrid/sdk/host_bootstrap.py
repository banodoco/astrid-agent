"""Process boundary for Astrid's one generic pack executor host.

The neutral runtime owns data and credentials.  This module only starts the
generic executor, waits for its registration/preflight readiness record, and
keeps a small support-directory marker so an Astrid relaunch reuses one host.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
from typing import Any, Callable, Mapping

from runtime_protocol import platform

from astrid.core.execution.host_log import rotate_if_needed
from astrid.core.execution.platform_helpers import signal_observed_group
from astrid.core.execution.process_group import (
    CleanupUncertainError,
    _process_snapshot,
    popen_owned_group,
    terminate_group,
)
from astrid.core.generation.vibecomfy_dependency import (
    VibeComfyDependencyError,
    dependency_pythonpath,
)

# Pack-host startup has no total-time cap: it waits while the host is alive and
# making progress (log growth, ready-file change, CPU use) and fails only when
# the host exits or shows no progress for this idle window.  A healthy host on
# a heavily loaded machine can take minutes; only a hung one should fail.
HOST_STARTUP_IDLE_SECONDS = 300.0
_PROGRESS_PROBE_INTERVAL_SECONDS = 1.0

PACK_HOST_ACTOR = "astrid-pack-host"
PACK_HOST_SCOPES = (
    "handshake",
    "worker:register",
    "worker:execute",
    "projects:read",
    "tasks:read",
    "objects:read",
    "objects:write",
)
PACK_HOST_PYTHON_ENV = "ASTRID_PACK_HOST_PYTHON"


class PackHostBootstrapError(RuntimeError):
    """A bounded, secret-free failure while starting the generic host."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "pack_host_bootstrap_failed",
        request_id: str = "",
        terminal: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.terminal = terminal

def _venv_interpreter_name(path: str) -> str:
    """One spelling per interpreter: `python`, `python3` and `python3.11` in one venv are the same host.

    The path is not resolved (that would collapse every venv onto its base
    Python), but aliases inside one venv's bin/ that are the same file are.
    Otherwise `astrid-tools` (`bin/python3`), `python -m astrid` and an SDK
    script each restart the shared pack host and drop its warm browser.
    """
    candidate = Path(path)
    bindir = candidate.parent
    if not (bindir.parent / "pyvenv.cfg").is_file():
        return path
    try:
        real = os.path.realpath(candidate)
        for name in ("python", "python3"):
            alias = bindir / name
            if alias.exists() and os.path.realpath(alias) == real:
                return str(alias)
    except OSError:
        pass
    return path


def _pack_host_python_executable() -> str:
    """Return the selected pack-host interpreter without resolving venv links."""
    configured = os.environ.get(PACK_HOST_PYTHON_ENV, "").strip()
    if not configured:
        return _venv_interpreter_name(os.path.abspath(sys.executable))

    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        raise PackHostBootstrapError(
            f"{PACK_HOST_PYTHON_ENV} must be an absolute path; field={PACK_HOST_PYTHON_ENV}"
        )
    try:
        executable = candidate.is_file() and os.access(candidate, os.X_OK)
    except (OSError, ValueError):
        executable = False
    if not executable:
        raise PackHostBootstrapError(
            f"{PACK_HOST_PYTHON_ENV} path={str(candidate)[:300]} is not an executable file"
        )
    return _venv_interpreter_name(os.path.abspath(str(candidate)))

def _host_pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if sys.platform == "win32":
        return platform.process_identity(value) is not None
    if sys.platform == "darwin":
        from astrid.core.execution.custody_broker import ordinary_candidate_observation
        observation = ordinary_candidate_observation(value)
        if observation["outcome"] in {"ABSENT", "ZOMBIE"}:
            return False
        if observation["outcome"] != "LIVE":
            raise PackHostBootstrapError("host native state observation unavailable")
        return True
    try:
        if platform.process_identity(value) is None:
            raise ProcessLookupError(value)
    except PermissionError:
        return True
    except OSError:
        return False
    # A terminated process can remain as a zombie until its original parent
    # reaps it.  It is not a live host and must not block a fresh launch.  The
    # Linux /proc probe is not available on macOS, which is the primary local
    # deployment, so keep a portable ``ps`` fallback as well.  Without this,
    # a host killed alongside an interrupted CLI remains recorded forever and
    # every next launch attempts to terminate the zombie until it times out.
    try:
        stat = Path(f"/proc/{value}/stat").read_text(encoding="ascii")
        state = stat.rsplit(")", 1)[-1].lstrip().split(None, 1)[0]
        if state == "Z":
            return False
    except (OSError, UnicodeDecodeError, IndexError):
        try:
            # ``subprocess.run`` is deliberately avoided here: the bootstrap
            # tests (and some embedded launchers) replace ``Popen`` while
            # modelling host startup.  The PID is already parsed as an int,
            # so this small, read-only ps probe has no shell-input surface.
            with os.popen(f"/bin/ps -p {value} -o state=", "r") as probe:
                state_text = probe.read()
            state = state_text.strip().split(None, 1)[0] if state_text.strip() else ""
            if state.upper().startswith("Z"):
                return False
        except (OSError, ValueError, IndexError):
            pass
    return True


def _launch_argv_digest(argv: tuple[str, ...] | list[str]) -> str:
    from astrid.core.execution.custody_broker import _canonical, _digest_bytes
    if "--max-concurrency" in argv:
        index = argv.index("--max-concurrency")
        if (argv.count("--max-concurrency") != 1 or index + 1 >= len(argv)
                or not re.fullmatch(r"[1-9][0-9]*", argv[index + 1])):
            raise PackHostBootstrapError("host launch capacity is invalid")
    return _digest_bytes(_canonical(list(argv)))


def _capture_native_host_launch_identity(pid: int) -> dict[str, int] | None:
    if sys.platform != "darwin":
        return None
    from astrid.core.execution.custody_broker import _darwin_process_records
    try:
        rows = _darwin_process_records(pid)
        if len(rows) != 1 or rows[0]["status"] not in {1, 2, 3, 4}:
            raise ValueError("no live launch identity")
        return {key: rows[0][key] for key in ("pid", "uid", "pgid", "start_sec", "start_usec")}
    except Exception as error:
        raise PackHostBootstrapError("host precise launch identity unavailable") from error


def _darwin_host_command_observation(pid: int, state: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Bounded command observation, never a cleanup or signaling capability."""
    from astrid.core.execution.custody_broker import _command_observation, _ps_lstart_birth
    observation = _command_observation(pid)
    if observation.get("outcome") != "BRACKETED_METADATA":
        raise PackHostBootstrapError("host native command observation unavailable; retirement blocked")
    before, after = observation["native_before"], observation["native_after"]
    metadata = observation["metadata_first"]
    bsd = metadata.get("bsd_identity", {})
    fields = ("pid", "uid", "parent_pid", "start_sec", "start_usec")
    argv = metadata.get("argv")
    if (before.get("status") not in {1, 2, 3, 4} or after.get("status") not in {1, 2, 3, 4}
            or before.get("pgid") != after.get("pgid")
            or any(before.get(key) != after.get(key) or bsd.get(key) != before.get(key) for key in fields)
            or not isinstance(argv, list) or not argv or not isinstance(argv[0], str)
            or not Path(argv[0]).is_absolute()
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(metadata.get("argv_sha256", "")))):
        raise PackHostBootstrapError("host native command identity unavailable; retirement blocked")
    if state is not None:
        pin = state.get("launch_argv_sha256")
        if (not isinstance(pin, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", pin)
                or before["pid"] != state.get("pid") or before["uid"] != platform.current_user()
                or _ps_lstart_birth(before["start_sec"]) != state.get("process_birth_id")
                or not isinstance(state.get("launch_native_identity"), Mapping)
                or any(state["launch_native_identity"].get(key) != before.get(key)
                       for key in ("pid", "uid", "pgid", "start_sec", "start_usec"))):
            raise PackHostBootstrapError("host native launch binding unavailable; retirement blocked")
        if metadata["argv_sha256"] != pin:
            # Framework Python may replace argv[0]. The exact original tail
            # remains private on the original retained Popen, never in a marker.
            process = _RETAINED_HOSTS.get((pid, str(state["process_birth_id"])))
            known = getattr(process, "_astrid_host_launch_argv", None)
            if (not isinstance(known, tuple) or not known or _launch_argv_digest(known) != pin
                    or not _host_executable_matches(argv[0], Path(known[0]))
                    or _launch_argv_digest((argv[0], *known[1:])) != metadata["argv_sha256"]):
                raise PackHostBootstrapError("host native command differs from its launch; retirement blocked")
    return observation


def _host_command(pid: Any) -> str:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return ""
    if sys.platform == "darwin":
        observed = _darwin_host_command_observation(value)
        return shlex.join(observed["metadata_first"]["argv"])
    try:
        return Path(f"/proc/{value}/cmdline").read_bytes().decode("utf-8", "ignore").replace("\x00", " ")
    except (OSError, ValueError):
        try:
            probe = subprocess.run(
                ["ps", "-p", str(value), "-o", "command="],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True, check=False, timeout=1.0,
            )
            return probe.stdout.strip()
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""


def _host_executable_matches(observed: str, selected: Path) -> bool:
    """Match the selected Python, including macOS framework launchers' exec.

    A framework's bin/python is a launcher, not a symlink to its Python.app
    executable. Ask that exact selected interpreter for its kernel executable
    instead of accepting a similarly named interpreter or a framework sibling.
    """
    if observed == str(selected):
        return True
    if sys.platform != "darwin" or not Path(observed).is_absolute():
        return False
    probe = (
        "import ctypes,os; "
        "library=ctypes.CDLL('/usr/lib/libproc.dylib'); "
        "buffer=ctypes.create_string_buffer(4096); "
        "size=library.proc_pidpath(os.getpid(),buffer,len(buffer)); "
        "assert size>0, 'process executable unavailable'; "
        "print(os.fsdecode(buffer.value))"
    )
    try:
        result = subprocess.run(
            [str(selected), "-X", "utf8", "-I", "-S", "-c", probe],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=True, timeout=2,
        )
        actual = Path(result.stdout.strip())
        return actual.is_absolute() and actual.is_file() and observed == str(actual)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _host_birth_identity(pid: Any) -> str:
    """Return the OS birth token for *pid*, or an empty value if unavailable."""
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if sys.platform == "win32":
        identity = platform.process_identity(value)
        return str(identity["birth_id"]) if identity is not None else ""
    try:
        info = _process_snapshot().get(value)
    except (CleanupUncertainError, OSError, ValueError):
        info = None
    return str(info.birth) if info is not None else ""


def _host_process_executable(pid: int) -> str | None:
    """Capture the kernel image path, bracketed by the process birth token."""
    if sys.platform != "win32":
        return None
    try:
        before = platform.process_identity(pid)
        if before is None:
            raise OSError("host process is absent")
        executable = platform.process_executable(pid)
        after = platform.process_identity(pid)
    except OSError as exc:
        raise PackHostBootstrapError("host native executable identity unavailable") from exc
    if after is None or before.get("birth_id") != after.get("birth_id"):
        raise PackHostBootstrapError("host process changed during executable identity capture")
    return str(executable)


def _runtime_service_launch_id(*, foreground: bool) -> str | None:
    """The Runtime supervisor's per-attempt binding for its foreground service."""
    if not foreground or os.environ.get("ASTRID_RUNTIME_SERVICE_NAME") != "pack-host":
        return None
    launch_id = os.environ.get("ASTRID_RUNTIME_SERVICE_LAUNCH_ID", "")
    if not launch_id:
        return None  # Older Runtime can still use the exact PID/birth receipt.
    if not re.fullmatch(r"[0-9a-f]{32}", launch_id):
        raise PackHostBootstrapError("Runtime pack-host service launch identity is malformed")
    return launch_id


def _host_identity_matches(state: Mapping[str, Any]) -> bool:
    """The registered pid, start time, group and command are this support root's host."""
    return _registered_host_status(state) == "ours"


def _our_host(pid: Any) -> bool:
    if sys.platform == "darwin":
        observation = _darwin_host_command_observation(int(pid))
        argv = observation["metadata_first"]["argv"]
        module = "astrid.core.execution.generic_host"
        opaque = "<opaque:sha256:" + hashlib.sha256(os.fsencode(module)).hexdigest() + ">"
        return any(argv[index] == "-m" and argv[index + 1] in {module, opaque}
                   for index in range(len(argv) - 1))
    return _host_pid_alive(pid) and "astrid.core.execution.generic_host" in _host_command(pid)


def _parked_host_identity_matches(state: Mapping[str, Any]) -> bool:
    """A parked host (verified inside its launching process): PID, birth, group and every launch binding."""
    pid = state.get("pid")
    if sys.platform == "darwin":
        try:
            if pid is None:
                raise ValueError("missing pid")
            value = int(pid)
        except (TypeError, ValueError) as exc:
            raise PackHostBootstrapError("host native launch identity invalid; retirement blocked") from exc
        observation = _darwin_host_command_observation(value, state)
        if observation["native_before"]["pgid"] != value:
            raise PackHostBootstrapError("host native process group changed; retirement blocked")
        return True
    if not _host_pid_alive(pid):
        return False
    expected_birth = str(state.get("process_birth_id") or "")
    if not expected_birth or _host_birth_identity(pid) != expected_birth:
        return False
    if not _our_host(pid):
        return False
    try:
        if platform.process_group(int(pid)) != int(pid):
            return False
    except (OSError, TypeError, ValueError):
        return False
    command = _host_command(pid)
    required = (
        "astrid.core.execution.generic_host",
        str(state.get("ready_file") or ""),
        str(state.get("source_checkout") or ""),
        str(state.get("credential_file") or ""),
        str(state.get("support_root") or ""),
        str(state.get("endpoint") or ""),
        str(state.get("source_closure_digest") or ""),
        str(state.get("boot_manifest_path") or ""),
        str(state.get("boot_manifest_hash") or ""),
    )
    return all(value and value in command for value in required)


def _host_failure_cause(log_path: Path, *, max_chars: int = 400) -> str:
    """Return only explicitly safe startup context from the child log."""
    try:
        with Path(log_path).open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if stripped.startswith("generic host CLI exiting after failure:"):
            if "safe_context=" in stripped:
                return stripped.split("safe_context=", 1)[1][:max_chars]
            return ""
        if stripped.startswith((
            "PackHostBootstrapError:",
            "astrid.sdk.host_bootstrap.PackHostBootstrapError:",
        )):
            return stripped[:max_chars]
    return ""


def _host_progress_token(process: Any, log_path: Path, ready_path: Path) -> Any:
    """Opaque value that changes whenever the starting host shows progress.

    Combines log size, ready-file stat and the process group's accumulated CPU
    time, so a host silently importing under load still counts as progressing.
    Never raises; unobservable parts are simply absent.
    """
    parts: list[Any] = []
    for path in (log_path, ready_path):
        try:
            st = Path(path).stat()
            parts.append((st.st_size, st.st_mtime_ns))
        except OSError:
            parts.append(None)
    try:
        out = subprocess.run(["ps", "-axo", "pgid=,cputime="], stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, check=False, timeout=5.0).stdout
        group = str(getattr(process, "pid", ""))
        parts.append(tuple(sorted(
            f[1] for f in (line.split() for line in out.splitlines())
            if len(f) == 2 and f[0] == group)))
    except (OSError, ValueError, subprocess.SubprocessError):
        parts.append(None)
    return tuple(parts)


def _host_start_phase(log_path: Path) -> str:
    """Last startup phase the host reported in its log, for failure messages."""
    try:
        with Path(log_path).open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 65536))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return "waiting for host ready file"
    for line in reversed(tail.splitlines()):
        if "astrid.host_startup_phase" in line:
            try:
                row = json.loads(line)
                return f"host startup phase {row.get('phase')}"
            except (ValueError, AttributeError):
                break
    return "waiting for host ready file"


def _read_object(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _write_object(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        temporary.write_text(json.dumps(dict(value), sort_keys=True), encoding="utf-8")
        platform.owner_only(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _dependency_pythonpath() -> tuple[str, ...]:
    """Keep only approved dependency roots across the host boundary."""
    try:
        return dependency_pythonpath()
    except VibeComfyDependencyError as exc:
        raise PackHostBootstrapError(
            "VibeComfy dependency paths could not be verified; field=PYTHONPATH"
        ) from exc


def _provision_render_runtime_env(
    source_path: Path, child_env: dict[str, str]
) -> None:
    """Bind a trusted local source profile to its concrete render toolchain.

    Packaged deployments may supply all three absolute settings themselves.
    For a source checkout, the launcher turns its installed Remotion bundle,
    schema package, and resolved Node executable into the same explicit
    settings before strict host preflight runs.
    """
    from astrid.core.env_vars import (
        ASTRID_NODE_EXECUTABLE,
        ASTRID_REMOTION_PROJECT_DIR,
        ASTRID_TIMELINE_SCHEMA_PYTHONPATH,
    )

    project_dir = (source_path / "remotion").resolve()
    if (
        ASTRID_REMOTION_PROJECT_DIR not in child_env
        and (project_dir / "package.json").is_file()
        and (project_dir / "node_modules").is_dir()
    ):
        child_env[ASTRID_REMOTION_PROJECT_DIR] = str(project_dir)

    schema_root = (
        project_dir
        / "node_modules"
        / "@banodoco"
        / "timeline-schema"
        / "python"
    )
    if (
        ASTRID_TIMELINE_SCHEMA_PYTHONPATH not in child_env
        and (schema_root / "banodoco_timeline_schema" / "__init__.py").is_file()
    ):
        child_env[ASTRID_TIMELINE_SCHEMA_PYTHONPATH] = str(schema_root.resolve())

    if (project_dir / "package.json").is_file() and (project_dir / "node_modules").is_dir():
        # One resolver (core/rendering/node_pin): the project's engines pin picks the renderer's
        # Node; ASTRID_NODE_EXECUTABLE is only an explicit override and must match it. A different
        # Node on PATH is never used silently, so the shell that launches the host cannot matter.
        from astrid.core.rendering.node_pin import resolve_pinned_node

        try:
            resolution = resolve_pinned_node(
                project_dir,
                override=(child_env.get(ASTRID_NODE_EXECUTABLE) or "").strip() or None,
                search_path=child_env.get("PATH"),
            )
        except Exception as exc:  # noqa: BLE001 - optional renderer setup must not stop unrelated host work
            # Node discovery is an optional render setup step. Leave the host
            # running, but ensure preflight sees rendering as unavailable.
            child_env.pop(ASTRID_NODE_EXECUTABLE, None)
            reason = " ".join(str(exc).split())[:300] or type(exc).__name__
            logging.getLogger(__name__).warning(
                "render Node setup failed; renderer will be unavailable: %s", reason
            )
            return
        if resolution.ok:
            child_env[ASTRID_NODE_EXECUTABLE] = str(resolution.path)
        # Missing or invalid tools are reported by render preflight; the host can still run other work.
    elif ASTRID_NODE_EXECUTABLE not in child_env:
        node = shutil.which("node", path=child_env.get("PATH"))
        if node:
            child_env[ASTRID_NODE_EXECUTABLE] = str(Path(node).resolve())


# Retained Popen handles of *parked* hosts (the activation flow, which launches and
# verifies a host inside one process).  The ordinary pack host is never gated on
# who launched it: it is identified by its registration (generic-host.json).
_RETAINED_HOSTS: dict[tuple[int, str], Any] = {}

_HOST_MODULE = "astrid.core.execution.generic_host"
_IDLE_CHECKS = 3
_IDLE_CHECK_INTERVAL_SECONDS = 1.0  # rides out the per-claim git preflight
_GROUP_GRACE_SECONDS = 3.0


def _descendant_snapshot(pid: int) -> dict[int, tuple[str, int]]:
    """Descendant birth/PGID pairs of *pid*; empty when it has no children."""
    try:
        snapshot = _process_snapshot()
    except (CleanupUncertainError, OSError, ValueError):
        return {}
    descendants: dict[int, tuple[str, int]] = {}
    frontier = [pid]
    while frontier:
        parent = frontier.pop()
        for info in snapshot.values():
            if info.ppid == parent and info.pid not in descendants:
                descendants[info.pid] = (str(info.birth), int(info.pgid))
                frontier.append(info.pid)
    return descendants


def _registered_host_status(state: Mapping[str, Any]) -> str:
    """Classify a host registration without any launcher-side custody.

    ``ours``    a live process with the registered pid AND start time, leading
                its own group, running the generic host for this support root;
    ``gone``    that process no longer exists (its group may hold orphans);
    ``reused``  the pid now belongs to another process (different start time) or
                the registration is unusable: nothing may be signalled;
    ``foreign`` the registered process is alive but is not this host.
    """
    raw_pid = state.get("pid")
    if raw_pid is None:
        return "reused"
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return "reused"
    birth = str(state.get("process_birth_id") or "")
    if pid <= 1 or not birth:
        return "reused"
    if sys.platform == "win32":
        # Windows has no POSIX process-group census. Runtime binds identity to
        # a kernel process handle (including the creation FILETIME), so use
        # that directly instead of treating the unsupported census as failure.
        # The registration itself is published in the support directory after
        # this host starts; requiring its exact creation token prevents a reused
        # PID from inheriting that registration.
        try:
            identity = platform.process_identity(pid)
        except OSError as exc:
            raise PackHostBootstrapError(
                "host native process identity unavailable; field=registered_host"
            ) from exc
        if identity is None:
            return "gone"
        if str(identity.get("birth_id") or "") != birth:
            return "reused"
        support_root = str(state.get("support_root") or "")
        expected_executable = str(state.get("python_executable") or "")
        if not support_root or not expected_executable:
            return "foreign"
        try:
            executable = platform.process_executable(pid)
            argv = _windows_process_argv(pid)
            after = platform.process_identity(pid)
        except OSError as exc:
            raise PackHostBootstrapError(
                "host native process metadata unavailable; field=registered_host"
            ) from exc
        if after is None:
            return "gone"
        if str(after.get("birth_id") or "") != birth:
            return "reused"
        expected_image = str(state.get("process_image") or "")
        if expected_image:
            if not _same_windows_path(executable, expected_image):
                return "foreign"
            allowed_argv_images = (expected_executable, expected_image)
        else:
            # Python 3.11's Windows venv redirector starts the base interpreter
            # with the base executable as argv[0]. The selected venv path is
            # still persisted in the registration, so for legacy markers that
            # lack process_image, bind the observed kernel image to either the
            # selected executable itself or the exact base executable recorded
            # by its adjacent pyvenv.cfg.
            base_executable = _windows_venv_base_executable(expected_executable)
            allowed_process_images = (expected_executable, base_executable)
            if not any(_same_windows_path(executable, value) for value in allowed_process_images if value):
                return "foreign"
            allowed_argv_images = (expected_executable, executable)
        if not argv or not any(_same_windows_path(argv[0], value) for value in allowed_argv_images if value):
            return "foreign"
        module_indexes = [index for index, value in enumerate(argv[:-1]) if value == "-m"]
        root_indexes = [index for index, value in enumerate(argv[:-1]) if value == "--support-root"]
        valid_python_prefix = (len(argv) > 3 and argv[1:3] == ["-X", "utf8"]
                               and (argv[3] == "-m" or (len(argv) > 4 and argv[3] == "-I" and argv[4] == "-m")))
        if (not valid_python_prefix or len(module_indexes) != 1
                or module_indexes[0] not in {3, 4}
                or argv[module_indexes[0] + 1] != _HOST_MODULE
                or len(root_indexes) != 1
                or not _same_windows_path(argv[root_indexes[0] + 1], support_root)):
            return "foreign"
        return "ours"
    try:
        info = _process_snapshot().get(pid)
    except CleanupUncertainError as exc:
        raise PackHostBootstrapError("process census unavailable; field=registered_host") from exc
    if info is None:
        return "gone"
    if str(info.birth) != birth:
        return "reused"
    if not _host_pid_alive(pid):
        return "gone"  # our registered leader, already dead and awaiting its parent
    support_root = str(state.get("support_root") or "")
    if info.pgid != pid or not support_root:
        return "foreign"
    command = _ps_command(pid)
    return "ours" if f" -m {_HOST_MODULE} " in f"{command} " and support_root in command else "foreign"


def _same_windows_path(left: str, right: str) -> bool:
    """Compare observed Windows executable paths using Windows path rules."""
    import ntpath

    try:
        if ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right)):
            return True
        return os.path.samefile(left, right)
    except (OSError, ValueError):
        return False


def _windows_venv_base_executable(selected_executable: str) -> str:
    """Read the base interpreter path for a Windows venv executable."""
    selected = Path(selected_executable)
    config = selected.parent.parent / "pyvenv.cfg"
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError, ValueError):
        return ""
    home = ""
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() == "home":
            home = value.strip()
            break
    if not home:
        return ""
    # Windows venvs normally use python.exe, even when a caller reaches the
    # same environment through python3.exe or pythonw.exe.
    for name in (selected.name, "python.exe"):
        candidate = Path(home) / name
        try:
            if candidate.is_file():
                return str(candidate)
        except (OSError, ValueError):
            continue
    return ""


def _windows_process_argv(pid: int) -> list[str]:
    """Read and parse a Windows process command using the native CIM interface.

    Win32_Process.CommandLine is read-only. The integer PID is validated by
    the caller and interpolated into a fixed WQL filter; JSON carries the
    returned command line without PowerShell quoting ambiguity.
    """
    query = (
        "$ErrorActionPreference='Stop'; "
        "$utf8=New-Object System.Text.UTF8Encoding $false; "
        "[Console]::OutputEncoding=$utf8; $OutputEncoding=$utf8; "
        f"$p=Get-CimInstance -ClassName Win32_Process -Filter 'ProcessId = {int(pid)}'; "
        "if ($null -eq $p -or $null -eq $p.CommandLine) { exit 3 }; "
        "$json=ConvertTo-Json -Compress -InputObject @{ProcessId=[int]$p.ProcessId; CommandLine=[string]$p.CommandLine}; "
        "[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))"
    )
    powershell = Path(os.environ.get("SystemRoot") or r"C:\Windows") / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    try:
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", query],
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise OSError("Windows process command observation failed") from exc
    if result.returncode != 0:
        raise OSError("Windows process command observation failed")
    try:
        # PowerShell 5.1 may write redirected stdout with the machine's OEM
        # code page even when Console.OutputEncoding is set. The JSON payload
        # is transported as ASCII Base64, then decoded as UTF-8 here so Unicode
        # command-line paths survive without depending on console settings.
        encoded = result.stdout.strip()
        if isinstance(encoded, str):
            encoded = encoded.encode("ascii")
        record = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        process_id = record.get("ProcessId") if isinstance(record, dict) else None
        command_line_value = record.get("CommandLine") if isinstance(record, dict) else None
        if (not isinstance(process_id, int) or process_id != int(pid)
                or not isinstance(command_line_value, str) or not command_line_value):
            raise ValueError("malformed process command")
        command_line = command_line_value
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OSError("Windows process command observation was malformed") from exc
    return _split_windows_command_line(command_line)


def _split_windows_command_line(command: str) -> list[str]:
    """Parse a Windows process command line using CommandLineToArgvW."""
    import ctypes
    from ctypes import wintypes

    win_dll = getattr(ctypes, "WinDLL")
    win_error = getattr(ctypes, "WinError")
    get_last_error = getattr(ctypes, "get_last_error")
    shell32 = win_dll("shell32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    values = shell32.CommandLineToArgvW(command, ctypes.byref(count))
    if not values:
        raise win_error(get_last_error())
    try:
        return [values[index] for index in range(count.value)]
    finally:
        kernel32.LocalFree(values)


def _ps_command(pid: int) -> str:
    try:
        probe = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, check=False, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    return probe.stdout.strip()


def _group_survivors(pgid: int, *, leader_birth: str | None = None) -> list[int]:
    """Live members of *pgid* (this process excluded)."""
    try:
        census = _process_snapshot()
    except CleanupUncertainError as exc:
        raise PackHostBootstrapError("process census unavailable; field=process_group") from exc
    leader = census.get(pgid)
    observed_leader_birth = (str(leader.birth) if leader is not None else
                             getattr(census, "exited", {}).get(pgid))
    if (leader_birth and observed_leader_birth is not None
            and (str(observed_leader_birth) != leader_birth
                 or (leader is not None and leader.pgid != pgid))):
        raise PackHostBootstrapError("registered host pid was reused; group not signalled")
    members = []
    for info in census.values():
        if info.pgid != pgid or info.pid == os.getpid():
            continue
        try:
            if _host_pid_alive(info.pid):
                members.append(info.pid)
        except PackHostBootstrapError:
            members.append(info.pid)
    return members


def _stop_group(pgid: int, *, grace: float | None = None, leader_birth: str | None = None) -> bool:
    """TERM, wait, KILL the anchored group; return whether SIGKILL was sent.

    The leader started its own session, so the group id equals the registered pid.
    Its birth token anchors the group. If the pid now names another incarnation,
    the whole group is refused.
    """
    if pgid <= 1 or pgid == platform.process_group(os.getpid()):
        raise PackHostBootstrapError("refusing to signal an unsafe process group")
    grace = _GROUP_GRACE_SECONDS if grace is None else grace
    forced = False
    for sig, wait in ((platform.TERM, grace), (platform.KILL, 3.0)):
        if not _group_survivors(pgid, leader_birth=leader_birth):
            return forced
        if not leader_birth:
            raise PackHostBootstrapError("registered host group lacks a leader birth anchor")
        try:
            signal_observed_group(pgid, sig, leader_birth=leader_birth)
            if sig == platform.KILL:
                forced = True
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise PackHostBootstrapError("prior host group cannot be signalled") from exc
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and _group_survivors(pgid):
            time.sleep(0.05)
    if _group_survivors(pgid):
        raise PackHostBootstrapError("prior host group survived SIGKILL; replacement blocked")
    return forced


def _runtime_cleanup_receipt_matches(state: Mapping[str, Any], *, foreground: bool) -> bool:
    """Prove Runtime drained the prior supervised pack-host Job before retry."""
    if (sys.platform != "win32" or not foreground
            or os.environ.get("ASTRID_RUNTIME_SERVICE_NAME") != "pack-host"):
        return False
    pid = state.get("pid")
    birth = str(state.get("process_birth_id") or "")
    support_root = str(state.get("support_root") or "")
    if not isinstance(pid, int) or pid <= 1 or not birth or not support_root:
        return False
    path = Path(support_root) / "pack-host-job-cleanup.json"
    lock_path = path.with_name(".pack-host-job-cleanup.lock")
    if (path.is_symlink() or lock_path.is_symlink() or not path.is_file()
            or not platform.is_owner_only(path)):
        return False
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a+") as lock:
            platform.owner_only(lock.fileno())
            platform.lock_file(lock.fileno())
            record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return False
    if (not isinstance(record, dict) or record.get("version") != 1
            or record.get("service_name") != "pack-host"
            or not _same_windows_path(str(record.get("support_root") or ""), support_root)):
        return False
    receipts = record.get("receipts")
    if not isinstance(receipts, list):
        return False
    return any(
        isinstance(item, dict)
        and _runtime_receipt_matches_state(item, state, support_root)
        and item.get("job_drained") is True
        and item.get("service_name", "pack-host") == "pack-host"
        for item in receipts
    )


def _runtime_receipt_matches_state(item: Mapping[str, Any], state: Mapping[str, Any], support_root: str) -> bool:
    if ("runtime_service_launch_id" in state
            and state.get("runtime_service_launch_id") is not None):
        launch_id = str(state.get("runtime_service_launch_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", launch_id):
            return False
        identity_match = item.get("runtime_service_launch_id") == launch_id
        receipt_pid, receipt_birth = item.get("pid"), item.get("process_birth_id")
        receipt_identity_valid = (
            (receipt_pid is None and receipt_birth is None)
            or (isinstance(receipt_pid, int) and receipt_pid > 1
                and isinstance(receipt_birth, str) and bool(receipt_birth))
        )
    else:
        identity_match = (item.get("pid") == state.get("pid")
                          and item.get("process_birth_id") == state.get("process_birth_id"))
        receipt_identity_valid = (
            isinstance(item.get("pid"), int) and item["pid"] > 1
            and isinstance(item.get("process_birth_id"), str) and bool(item["process_birth_id"])
        )
    return (
        identity_match
        and receipt_identity_valid
        and _same_windows_path(str(item.get("support_root", support_root)), support_root)
    )


def _consume_runtime_cleanup_receipt(state: Mapping[str, Any]) -> None:
    """Remove the exact receipt after its matching stale markers are retired."""
    support_root = str(state.get("support_root") or "")
    path = Path(support_root) / "pack-host-job-cleanup.json"
    lock_path = path.with_name(".pack-host-job-cleanup.lock")
    if (path.is_symlink() or lock_path.is_symlink() or not path.is_file()
            or not platform.is_owner_only(path)):
        return
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a+") as lock:
            platform.owner_only(lock.fileno())
            platform.lock_file(lock.fileno())
            record = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(record, dict) or record.get("version") != 1
                    or record.get("service_name") != "pack-host"
                    or not _same_windows_path(str(record.get("support_root") or ""), support_root)
                    or not isinstance(record.get("receipts"), list)):
                return
            remaining = [item for item in record["receipts"] if not (
                isinstance(item, dict) and _runtime_receipt_matches_state(item, state, support_root)
                and item.get("job_drained") is True
                and item.get("service_name", "pack-host") == "pack-host"
            )]
            if len(remaining) != len(record["receipts"]):
                _write_object(path, {**record, "receipts": remaining})
    except (OSError, ValueError, ImportError):
        return


def _terminate_old_host(state: Mapping[str, Any], *, foreground: bool = False,
                        status: str | None = None) -> bool:
    """Stop the registered host group and return whether shutdown sent SIGKILL."""
    status = _registered_host_status(state) if status is None else status
    if status == "foreign":
        raise PackHostBootstrapError("registered pid is not this Astrid pack host; not signalled")
    if sys.platform == "win32":
        # Windows tree termination is authorized by a retained Job handle, not
        # the numeric process ID. Only the process that launched the host can
        # hold that capability; a later process cannot reconstruct it from the
        # registration or PID, even when the root has exited.
        retained = _RETAINED_HOSTS.get((int(state["pid"]), str(state.get("process_birth_id") or "")))
        if retained is not None:
            try:
                terminate_group(retained)
            except CleanupUncertainError as exc:
                raise PackHostBootstrapError(
                    "registered Windows host Job cleanup is unproved; retirement blocked"
                ) from exc
            return False
        if status in {"gone", "reused"} and _runtime_cleanup_receipt_matches(state, foreground=foreground):
            return False
        if status in {"gone", "reused"} and foreground:
            raise PackHostBootstrapError(
                "legacy Windows host registration lacks a matching Runtime Job-drain receipt; "
                "automatic retirement is blocked"
            )
        raise PackHostBootstrapError(
            "registered Windows host has no retained Job custody; retirement blocked"
        )
    if status != "reused":
        return _stop_group(int(state["pid"]), leader_birth=str(state.get("process_birth_id") or "") or None)
    return False


def _retire_host_locked(state: Mapping[str, Any], *, foreground: bool = False) -> None:
    """Caller holds the host lock. Stop the registered host, drop its markers."""
    support = Path(str(state.get("support_root") or ""))
    status = _registered_host_status(state)
    used_receipt = (
        sys.platform == "win32" and status in {"gone", "reused"}
        and _runtime_cleanup_receipt_matches(state, foreground=foreground)
    )
    if sys.platform == "win32" and status == "foreign":
        raise PackHostBootstrapError("registered Windows host identity is foreign; retirement blocked")
    if status != "foreign":
        if foreground:
            _terminate_old_host(state, foreground=True, status=status)
        else:
            _terminate_old_host(state)
    for name in ("generic-host.ready.json", "generic-host.json"):
        (support / name).unlink(missing_ok=True)
    if used_receipt:
        _consume_runtime_cleanup_receipt(state)


def _stop_registered_host(state: Mapping[str, Any]) -> None:
    """Retire a registered host under its lifecycle lock (any Astrid process may)."""
    try:
        lock_path = Path(str(state.get("support_root") or "")) / "generic-host.lock"
        with lock_path.open("a+") as lock:
            platform.owner_only(lock.fileno())
            platform.lock_file(lock.fileno())
            _retire_host_locked(state)
    except (ImportError, OSError) as exc:
        raise PackHostBootstrapError("prior host registration lock is unavailable; retirement blocked") from exc


def _host_idle(pid: int, support_root: Path | None = None) -> bool:
    """A host is idle when it executes no task.

    Hosts publish their in-flight task count (generic-host.busy.json). A host that
    predates that record is judged by its children outside its own process group:
    task work runs in its own session, while the host's short helpers (the per-claim
    git preflight) share the host's group and are not work.
    """
    if support_root is not None:
        try:
            record = json.loads((support_root / "generic-host.busy.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if isinstance(record, dict) and record.get("pid") == pid:
            return int(record.get("active") or 0) == 0
    for check in range(_IDLE_CHECKS):
        if check:
            time.sleep(_IDLE_CHECK_INTERVAL_SECONDS)
        if any(pgid != pid for _birth, pgid in _descendant_snapshot(pid).values()):
            return False
    return True


def _execution_profile() -> str:
    profile = os.environ.get("ASTRID_HOST_EXECUTION_PROFILE", "source").strip()
    if profile not in {"source", "installed"}:
        raise PackHostBootstrapError("host execution profile must be source or installed")
    return profile


def _capability_matrix_path(source_path: Path) -> Path:
    """Select the installed matrix without accepting a symlinked override."""
    from astrid.core.env_vars import ASTRID_CAPABILITY_MATRIX_PATH, ASTRID_SOURCE_PROVENANCE_ROOT

    raw = os.environ.get(ASTRID_CAPABILITY_MATRIX_PATH, "").strip()
    if not raw:
        return source_path / "config" / "astrid-beta-capabilities.json"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise PackHostBootstrapError("installed capability matrix path must be absolute")
    path = Path(path.absolute())
    current = path
    while True:
        if current.is_symlink():
            raise PackHostBootstrapError("installed capability matrix path must not contain symlinks")
        if current == current.parent:
            break
        current = current.parent
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PackHostBootstrapError("installed capability matrix path is unavailable") from exc
    provenance = os.environ.get(ASTRID_SOURCE_PROVENANCE_ROOT, "").strip()
    if provenance:
        root = Path(provenance).expanduser()
        if not root.is_absolute():
            raise PackHostBootstrapError("installed capability matrix is outside its source provenance root")
        try:
            provenance_root = root.resolve(strict=True)
        except OSError as exc:
            raise PackHostBootstrapError("installed source provenance root is unavailable") from exc
        if not resolved.is_relative_to(provenance_root):
            raise PackHostBootstrapError("installed capability matrix is outside its source provenance root")
    if not resolved.is_file():
        raise PackHostBootstrapError("installed capability matrix is not a regular file")
    return resolved


def _installed_execution_origins(source_path: Path, python_executable: str) -> dict[str, str]:
    """Probe the selected interpreter in isolation, without a checkout path.

    The profile must bind the installed product root as its execution source.
    An old provenance checkout is diagnostic data, never import authority.
    """
    probe = """import importlib.util,json
modules={'host':'astrid.core.execution.generic_host','child':'astrid.core.execution.generic_host_worker','action':'astrid.packs.rendering.actions.render.run','client':'astrid_runtime_client','sdk':'astrid.sdk.invocation'}
print(json.dumps({key:importlib.util.find_spec(module).origin for key,module in modules.items()}))
"""
    completed = subprocess.run([python_executable, "-X", "utf8", "-I", "-c", probe],
                               stdin=subprocess.DEVNULL,
                               cwd=tempfile.gettempdir(), capture_output=True, text=True,
                               timeout=20, check=False,
                               env={key: value for key, value in os.environ.items()
                                    if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}})
    try:
        origins = json.loads(completed.stdout) if completed.returncode == 0 else None
        if not isinstance(origins, dict) or set(origins) != {"host", "child", "action", "client", "sdk"}:
            raise ValueError("missing installed origins")
        for name, origin in origins.items():
            path = Path(origin)
            if not path.is_absolute() or not path.is_file() or not path.resolve().is_relative_to(source_path.resolve()):
                raise ValueError(f"{name} is outside the selected installed product tree")
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise ValueError(f"{name} has a source alias")
        return {name: str(Path(origin).resolve()) for name, origin in origins.items()}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PackHostBootstrapError(
            "installed host profile does not bind actual installed host/child/action/client origins; "
            "rebind the profile to the selected interpreter's noneditable product tree") from exc


_JOB_ENV_TAGS = ("OTTO_HEAVY_LEASE", "OTTO_HEAVY_DIR", "OTTO_HEAVY_CAPACITY")


def _host_render_environment(source_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop('ASTRID_PACKS_PATH', None)
    # The host outlives the command (and any capacity job) that happened to start it.
    # Lease tags and private capacity state belong to that job, not the durable host.
    for tag in _JOB_ENV_TAGS:
        environment.pop(tag, None)
    if _execution_profile() == "installed":
        _installed_execution_origins(source_path, _pack_host_python_executable())
        environment.pop("PYTHONPATH", None)
        # Launching from an unrelated directory avoids Python's empty-path
        # source preference; installed dependencies must belong to the venv.
    else:
        environment['PYTHONPATH'] = os.pathsep.join((str(source_path), *_dependency_pythonpath()))
    _provision_render_runtime_env(source_path, environment)
    return environment


def _verify_remotion_probe_custody(environment: Mapping[str, str]) -> None:
    from astrid.core.rendering.remotion_runtime import (
        RemotionProbeCleanupError,
        remotion_runtime_status,
    )

    try:
        # Optional rendering failures are handled by capability preflight;
        # unresolved probe custody must stop the host before it is spawned.
        remotion_runtime_status(require_explicit_project=True, environment=environment)
    except RemotionProbeCleanupError as exc:
        raise PackHostBootstrapError(
            "Remotion probe cleanup is unproved; pack host launch is blocked"
        ) from exc


# Launch state the owner holds about a parked host that the host never writes into its ready marker:
# the process identity is compared field by field, and the launch argv pin and native launch identity
# are verified against the live process by _parked_host_identity_matches.
_LAUNCH_ONLY_STATE = frozenset({"pid", "process_birth_id", "launch_argv_sha256", "launch_native_identity"})


@dataclass
class _ParkedPackHostHandle:
    process: Any
    control: socket.socket
    reference: Any
    state: dict[str, Any]
    operation_id: str
    channel_id: str
    grant_sent: bool = False
    grant: Mapping[str, Any] | None = None
    cleanup_error: BaseException | None = None
    aborted: bool = False
    execution_profile: str = "source"
    execution_origins: Mapping[str, str] | None = None


class _ParkedPackHostPreparer:
    """Private local adapter for Runtime's existing launch projection/callback.

    The projection is injected by the resident owner; this module imports no
    Runtime store or authority implementation. Preparation returns while the
    canonical host is parked, before registration or credential consumption.
    """

    def __init__(self, project_launch: Callable, *, timeout_seconds: float = 20,
                 verify_target: Callable | None = None) -> None:
        if not 0 < timeout_seconds <= 900:
            raise ValueError("parked host timeout is invalid")
        self.project_launch = project_launch
        self.timeout_seconds = timeout_seconds
        self.verify_target = verify_target
        self._handle: _ParkedPackHostHandle | None = None

    def prepare(self, reference: Any) -> _ParkedPackHostHandle:
        if sys.platform == "win32":
            raise PackHostBootstrapError("parked pack-host preparation is unavailable on Windows")
        launch = getattr(reference, "launch", None)
        reference = getattr(reference, "reference", reference)
        if self._handle is not None:
            raise PackHostBootstrapError("parked host preparer already owns an incarnation")
        if self.verify_target is not None:
            self.verify_target(reference.effective_target)
        elif reference.effective_target != {"kind": "machine", "id": socket.gethostname()}:
            raise PackHostBootstrapError("local preparer requires verified local placement")
        if (reference.source_checkout is None or not reference.pack_roots
                or reference.ready_file is None or Path(reference.ready_file).exists()):
            raise PackHostBootstrapError("explicit local source, placement and fresh ready path are required")
        launch = launch or self.project_launch(reference)
        if launch.deployment_digest != reference.digest():
            raise PackHostBootstrapError("parked host launch projection is foreign")
        from astrid.core.subprocess_env import build_child_subprocess_env

        projected_env = dict(launch.env())
        projected_env.pop("ASTRID_CREDENTIAL_REF", None)  # Already bound by --credential-file.
        projected_env.pop("ASTRID_IDEMPOTENCY_KEY", None)  # Admission remains with Runtime.
        # Keep the launch projection isolated from ambient Astrid task/session
        # state. On macOS, however, the child's CoreFoundation encoding pin
        # must match the owner process. Pass only that inherited pin through
        # the policy's parent channel; a conflicting projected value still
        # fails closed.
        parent_env: dict[str, str] = {}
        if sys.platform == "darwin":
            cf_encoding = os.environ.get("__CF_USER_TEXT_ENCODING")
            if cf_encoding is not None:
                parent_env["__CF_USER_TEXT_ENCODING"] = cf_encoding
        env = build_child_subprocess_env(parent=parent_env, explicit_env=projected_env)
        env.pop("ASTRID_PACKS_PATH", None)
        if _execution_profile() == "installed":
            origins = _installed_execution_origins(reference.source_checkout, str(launch.argv[0]))
            env.pop("PYTHONPATH", None)
        else:
            origins = {}
            env["PYTHONPATH"] = os.pathsep.join((
                str(reference.source_checkout), *_dependency_pythonpath(),
                *(str(item.path) for item in reference.dependency_closure if item.path.is_dir()),
            ))
        env["PYTHONUNBUFFERED"] = "1"
        _provision_render_runtime_env(reference.source_checkout, env)
        parent, child = socket.socketpair()
        operation_id, channel_id = secrets.token_hex(16), secrets.token_hex(16)
        launch_argv = list(launch.argv)
        if launch_argv[1:3] != ["-X", "utf8"]:
            launch_argv[1:1] = ["-X", "utf8"]
        if origins and "-I" not in launch_argv[1:]:
            launch_argv.insert(3, "-I")
        argv = [*launch_argv,
                "--source-closure-digest", reference.source_closure_digest.removeprefix("sha256:"),
                "--activation-fd", str(child.fileno()),
                "--activation-operation-id", operation_id,
                "--activation-channel-id", channel_id,
                "--activation-timeout-seconds", str(self.timeout_seconds)]
        log_path = Path(reference.support_root) / f"generic-host-{operation_id}.log"
        launch_digest = _launch_argv_digest(argv)
        try:
            with log_path.open("xb") as log:
                platform.owner_only(log.fileno())
                process = popen_owned_group(
                    argv, cwd=str(reference.support_root if origins else launch.cwd), env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, close_fds=True, pass_fds=(child.fileno(),),
                )
        except Exception:
            parent.close()
            raise
        finally:
            child.close()
        setattr(process, "_astrid_host_launch_argv", tuple(argv))
        state = {
            "launch_argv_sha256": launch_digest,
            "pid": process.pid, "process_birth_id": _host_birth_identity(process.pid),
            "ready_file": str(reference.ready_file), "source_checkout": str(reference.source_checkout),
            "credential_file": reference.credential_ref.removeprefix("file:"),
            "support_root": str(reference.support_root), "endpoint": reference.runtime_endpoint,
            "source_closure_digest": reference.source_closure_digest.removeprefix("sha256:"),
            "boot_manifest_path": str(reference.boot_manifest_path),
            "boot_manifest_hash": reference.boot_manifest_hash.removeprefix("sha256:"),
        }
        _RETAINED_HOSTS[(process.pid, state["process_birth_id"])] = process
        try:
            state["launch_native_identity"] = _capture_native_host_launch_identity(process.pid)
        except BaseException as primary:
            parent.close()
            try:
                terminate_group(process)
            except BaseException as cleanup:
                raise primary from cleanup
            raise
        # Launch diagnostics are outside the C06 ready identity contract.
        handle = _ParkedPackHostHandle(process, parent, reference, state, operation_id, channel_id,
                                       execution_profile=_execution_profile(), execution_origins=dict(origins))
        self._handle = handle
        if not state["process_birth_id"]:
            parent.close()
            terminate_group(process)
            raise PackHostBootstrapError("parked host birth identity could not be captured")
        return handle

    def _assert_owned(self, handle: object) -> _ParkedPackHostHandle:
        if not isinstance(handle, _ParkedPackHostHandle) or handle is not self._handle:
            raise PackHostBootstrapError("parked host handle is foreign")
        return handle

    def acknowledge(self, handle: object, grant: Mapping[str, Any], *, accept: Callable | None = None) -> Mapping[str, Any]:
        from astrid.core.execution.generic_host import (
            _ACTIVATION_ACCEPTED_VERSION,
            _ACTIVATION_BOOTSTRAP_MODE,
            _ACTIVATION_RECEIPT_MODE,
            _ACTIVATION_RECEIPT_VERSION,
            _ACTIVATION_REQUEST_VERSION,
            _ACTIVATION_VERSION,
            _read_activation_frame,
            _send_activation_frame,
        )
        handle = self._assert_owned(handle)
        if handle.grant_sent or handle.process.poll() is not None or not _parked_host_identity_matches(handle.state):
            exit_code = handle.process.poll()
            detail = ""
            if exit_code is not None:
                log_path = Path(handle.state["support_root"]) / f"generic-host-{handle.operation_id}.log"
                detail = f" (host exited {exit_code}: {_host_failure_cause(log_path) or 'see ' + str(log_path)})"
            raise PackHostBootstrapError("parked host grant is consumed or its incarnation changed" + detail)
        if (set(grant) != {"activation_id", "credential_file", "executor_incarnation", "evidence_digest"}
                or grant["credential_file"] != handle.state["credential_file"]):
            raise PackHostBootstrapError("parked host grant is foreign")
        credential_path = Path(grant["credential_file"])
        if (credential_path.is_symlink() or not credential_path.is_file()
                or not platform.is_owner_only(credential_path)):
            raise PackHostBootstrapError("parked host credential reference is unsafe")
        process = {"pid": handle.process.pid, "birth_id": handle.state["process_birth_id"]}
        # Freeze the exact grant before delivery; callback code cannot mutate
        # the expected request/receipt by retaining an alias to this mapping.
        frozen_grant = json.loads(json.dumps(dict(grant)))
        handle.grant = frozen_grant
        request = {"version": _ACTIVATION_REQUEST_VERSION, "operation_id": handle.operation_id,
                   "channel_id": handle.channel_id, "grant": frozen_grant, "host": process}
        handle.grant_sent = True  # A transport exception never permits resend.
        try:
            handle.control.settimeout(self.timeout_seconds)
            _send_activation_frame(handle.control, {
                **frozen_grant, "version": _ACTIVATION_VERSION,
                "acceptance_mode": _ACTIVATION_RECEIPT_MODE if accept is not None else _ACTIVATION_BOOTSTRAP_MODE,
                "operation_id": handle.operation_id, "channel_id": handle.channel_id, "host": process,
            })
            if accept is not None:
                received = _read_activation_frame(handle.control)
                if received != request or not _parked_host_identity_matches(handle.state):
                    raise PackHostBootstrapError("parked host acceptance is foreign or stale")
                accept(received["grant"], received["host"])
                _send_activation_frame(handle.control, {**request, "version": _ACTIVATION_RECEIPT_VERSION})
                handle.control.shutdown(socket.SHUT_WR)
            acknowledgement = _read_activation_frame(handle.control)
            expected = {
                "version": _ACTIVATION_ACCEPTED_VERSION, "operation_id": handle.operation_id,
                "channel_id": handle.channel_id, "activation_id": frozen_grant["activation_id"],
                "executor_incarnation": frozen_grant["executor_incarnation"],
                "evidence_digest": frozen_grant["evidence_digest"], "host": process,
            }
            if acknowledgement != expected or handle.control.recv(1):
                raise PackHostBootstrapError("parked host final acknowledgement is invalid")
            return {key: frozen_grant[key] for key in (
                "activation_id", "executor_incarnation", "evidence_digest",
            )}
        finally:
            handle.control.close()

    def await_ready(self, handle: object) -> None:
        from astrid.core.execution.generic_host import (
            RuntimeProtocolClient,
            _await_enabled_runtime_credential,
        )

        handle = self._assert_owned(handle)
        reference = handle.reference
        client = RuntimeProtocolClient(reference.runtime_endpoint, Path(handle.state["credential_file"]).read_text().strip())
        _await_enabled_runtime_credential(client, executor_id=reference.executor_id,
                                          timeout_seconds=self.timeout_seconds)
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            ready = _read_object(Path(reference.ready_file))
            if ready and ready.get("status") == "ready":
                if (not _parked_host_identity_matches(handle.state)
                        or ready.get("pid") != handle.process.pid
                        or ready.get("process_birth_id") != handle.state["process_birth_id"]
                        or ready.get("executor_id") != reference.executor_id
                        or any(ready.get(key) != value for key, value in handle.state.items()
                               if key not in _LAUNCH_ONLY_STATE)):
                    raise PackHostBootstrapError("parked host readiness is foreign")
                return
            if handle.process.poll() is not None or (ready and ready.get("status") == "failed"):
                break
            time.sleep(0.02)
        raise PackHostBootstrapError("parked host did not become ready")

    def abort(self, handle: object) -> None:
        handle = self._assert_owned(handle)
        if handle.aborted:
            return
        primary_error = sys.exc_info()[1]
        try:
            handle.control.close()
            terminate_group(handle.process)
            handle.process.wait(timeout=1)
            ready_path = Path(handle.state["ready_file"])
            ready = _read_object(ready_path)
            if ready and (ready.get("pid"), ready.get("process_birth_id")) == (
                    handle.process.pid, handle.state["process_birth_id"]):
                ready_path.unlink()
            handle.aborted = True
        except BaseException as cleanup_error:
            handle.cleanup_error = cleanup_error
            # Runtime must still observe abort failure and retain uncertainty.
            # Keep the initiating failure primary, with cleanup as its cause;
            # a failed identity observation grants no signaling authority.
            if primary_error is not None:
                raise primary_error.with_traceback(primary_error.__traceback__) from cleanup_error
            raise


def _host_artifact_digest(path: Path) -> str:
    if (not path.exists() or path.is_symlink()
            or (path.is_dir() and any(item.is_symlink() for item in path.rglob("*")))):
        raise PackHostBootstrapError("parked host artifact path is aliased")
    from astrid.core.execution.generic_host import _source_digest
    return "sha256:" + _source_digest(path).removeprefix("sha256:")


class _ParkedPackHostInspector:
    """Independent local OS/artifact witness; never trusts the ready marker."""

    def __init__(self, runtime_health: Callable, *, session_config_path: Path,
                 provider_observer: Callable | None = None,
                 executor_observer: Callable | None = None) -> None:
        self.runtime_health = runtime_health
        self.session_config_path = session_config_path
        self.provider_observer = provider_observer
        self.executor_observer = executor_observer

    def observe(self, handle: object) -> Mapping[str, Any]:
        from astrid.core.execution.generic_host import (
            _canonical_digest,
            source_checkout_closure_digest,
        )
        from astrid.core.generation.model_root import model_root_binding_from_profile

        if not isinstance(handle, _ParkedPackHostHandle) or not _parked_host_identity_matches(handle.state):
            raise PackHostBootstrapError("parked host OS identity changed")
        reference = handle.reference
        # The framework-launcher probe executes the selected interpreter. Verify
        # its admitted bytes before asking it to resolve the process image.
        if _host_artifact_digest(reference.executable.path) != reference.executable.digest:
            raise PackHostBootstrapError("parked host executable artifact changed")
        info = _process_snapshot().get(handle.process.pid)
        if sys.platform == "darwin":
            observation = _darwin_host_command_observation(handle.process.pid, handle.state)
            command = observation["metadata_first"]["argv"]
        else:
            command = shlex.split(_host_command(handle.process.pid))
        if sys.platform == "darwin":
            # The complete native census already carries the UID. Never create
            # a ps child inside the observed host's custody graph.
            after = _process_snapshot().get(handle.process.pid)
            if info is None or after != info or info.uid is None:
                raise PackHostBootstrapError("parked host native identity changed or is unavailable")
            uid = info.uid
        else:
            uid = subprocess.run(["ps", "-p", str(handle.process.pid), "-o", "uid="], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, check=True, timeout=1).stdout.strip()
        if (info is None or info.birth != handle.state["process_birth_id"]
                or not command or not _host_executable_matches(command[0], reference.executable.path)
                or int(uid) != platform.current_user()):
            raise PackHostBootstrapError("parked host executable, uid or birth identity changed")
        if sys.platform == "darwin":
            known = getattr(handle.process, "_astrid_host_launch_argv", None)
            if (not isinstance(known, tuple) or _launch_argv_digest(known) != handle.state["launch_argv_sha256"]
                    or known.count("--max-concurrency") != 1):
                raise PackHostBootstrapError("parked host known capacity binding unavailable")
            capacity = int(known[known.index("--max-concurrency") + 1])
        else:
            capacity = int(command[command.index("--max-concurrency") + 1])
        if capacity != reference.capacity:
            raise PackHostBootstrapError("parked host capacity changed")
        profile_path = Path(reference.readiness_profile_path)
        if ("sha256:" + hashlib.sha256(profile_path.read_bytes()).hexdigest()) != reference.readiness_profile_hash:
            raise PackHostBootstrapError("parked host readiness profile changed")
        profile = _read_object(profile_path)
        binding = model_root_binding_from_profile(profile, verify_files=True)
        session = _read_object(self.session_config_path)
        if binding.path != reference.model_root or not session or session.get("session_ref") != reference.session_ref:
            raise PackHostBootstrapError("parked host model root or session changed")
        dependencies = []
        for item in reference.dependency_closure:
            digest = _host_artifact_digest(item.path)
            if digest != item.digest:
                raise PackHostBootstrapError("parked host dependency changed")
            dependencies.append({"name": item.name, "path": str(item.path), "digest": digest})
        health = self.runtime_health()
        def field(name):
            return health.get(name) if isinstance(health, Mapping) else getattr(health, name, None)
        result = {
            "target": {"kind": "machine", "id": socket.gethostname()},
            "machine_identity": {"id": socket.gethostname(), "uid": int(uid)},
            "process": {"pid": info.pid, "birth_id": info.birth, "pgid": info.pgid,
                        "sid": platform.session_id(info.pid), "uid": int(uid), "executable": command[0],
                        "artifact_digest": _host_artifact_digest(Path(command[0]))},
            "runtime_instance_id": field("runtime_instance_id"), "runtime_epoch": field("runtime_epoch"),
            "runtime_session_id": field("runtime_session_id"),
            "source_closure_digest": "sha256:" + source_checkout_closure_digest(reference.source_checkout),
            "dependency_closure_digest": "sha256:" + _canonical_digest(dependencies),
            "model_root": str(binding.path), "model_inventory_digest": binding.inventory_digest,
            "session_ref": session["session_ref"],
            "session_config_digest": "sha256:" + hashlib.sha256(self.session_config_path.read_bytes()).hexdigest(),
            "data_root": str(Path(reference.data_root).resolve(strict=True)),
            "support_root": str(Path(reference.support_root).resolve(strict=True)),
            "capacity": capacity,
        }
        # Every placement kind carries the output root: Runtime's observation contract requires it.
        output = Path(reference.output_root)
        if output.is_symlink() or not output.is_dir():
            raise PackHostBootstrapError("parked host output root is unsafe")
        result["output_root"] = str(output.resolve(strict=True))
        if self.provider_observer is not None:
            provider = self.provider_observer(handle)
            target = reference.effective_target
            if provider != {"account_ref": target.get("provider_account_ref"), "pod_id": target.get("pod_id")}:
                raise PackHostBootstrapError("parked host provider identity changed")
            result["target"] = dict(target)
            result.pop("machine_identity")
            result["provider_identity"] = dict(provider)
        return result

    def observe_ready(self, handle: Any) -> Mapping[str, Any]:
        """Combine OS/artifact measurements with authenticated resident state."""
        from astrid.core.execution.generic_host import (
            GenericPackHost,
            RuntimeProtocolClient,
            _canonical_digest,
        )

        observed = self.observe(handle)
        if self.executor_observer is None:
            raise PackHostBootstrapError("independent executor observer is required")
        reference = handle.reference
        ready = _read_object(Path(reference.ready_file))
        activation = ready.get("activation") if ready else None
        grant = handle.grant
        if (not ready or ready.get("status") != "ready" or not grant
                or ready.get("pid") != handle.process.pid
                or ready.get("process_birth_id") != handle.state["process_birth_id"]
                or ready.get("executor_id") != reference.executor_id
                or ready.get("output_root") != str(reference.output_root)
                or ready.get("attempt_base") != str(reference.output_root / "attempts")
                or ready.get("session_ref") != observed["session_ref"]
                or ready.get("session_config_digest") != observed["session_config_digest"]
                or ready.get("data_root") != observed["data_root"]
                or ready.get("identity_attestation", {}).get("target") != observed["target"]
                or ready.get("runtime_instance_id") != observed["runtime_instance_id"]
                or ready.get("runtime_epoch") != observed["runtime_epoch"]
                or ready.get("source_closure_digest") != reference.source_closure_digest.removeprefix("sha256:")
                or not isinstance(activation, Mapping)
                or activation.get("operation_id") != handle.operation_id
                or activation.get("channel_id") != handle.channel_id
                or activation.get("executor_incarnation") != grant["executor_incarnation"]
                or activation.get("evidence_digest") != grant["evidence_digest"]
                or activation.get("host") != {"pid": handle.process.pid, "birth_id": handle.state["process_birth_id"]}):
            raise PackHostBootstrapError("initialized host readiness is foreign")
        model_binding = ready.get("model_root_binding")
        if (not isinstance(model_binding, Mapping)
                or model_binding.get("path") != observed["model_root"]
                or model_binding.get("inventory_digest") != observed["model_inventory_digest"]):
            raise PackHostBootstrapError("initialized host model binding is foreign")
        client = RuntimeProtocolClient(reference.runtime_endpoint,
                                       Path(handle.state["credential_file"]).read_text().strip())
        client._authenticate_worker(reference.executor_id)
        registration = self.executor_observer(reference.executor_id)
        capability = reference.capability_identity
        records = GenericPackHost(pack_roots=list(reference.pack_roots)).discover()
        source_digest = _canonical_digest({item.id: item.source_digest for item in records})
        dependency_digest = _canonical_digest({item.id: item.dependency_digest for item in records})
        if (not isinstance(registration, Mapping)
                or registration.get("id") != reference.executor_id
                or registration.get("max_concurrency") != reference.capacity
                or registration.get("runtime_epoch") != observed["runtime_epoch"]
                or registration.get("readiness") != "ready"
                or registration.get("source_digest") != source_digest
                or registration.get("dependency_digest") != dependency_digest
                or not any(item.get("capability_id") == capability.capability_id
                           and item.get("definition_digest") == capability.capability_digest
                           and item.get("status") == "ready"
                           for item in registration.get("capabilities", []))):
            raise PackHostBootstrapError("authenticated executor registration is not ready")
        with tempfile.TemporaryFile(dir=reference.output_root) as probe:
            probe.write(b"ready")
            probe.flush()
        return observed




def _attach_resident_host(
    current: Mapping[str, Any], ready: Mapping[str, Any], *,
    expected: Mapping[str, Any], worker_path: Path, runtime_epoch: Any,
    runtime_instance_id: str, inventory_identity: str,
) -> Mapping[str, Any] | None:
    """Return a receipt for a live Runtime-registered host from another checkout."""
    checkout = Path(str(current.get("source_checkout") or ""))
    attachable_keys = (
        "endpoint", "executor_id", "ready_file", "credential_file",
        "support_root", "runtime_instance_id", "runtime_epoch", "schema_digest",
    )
    if not (
        checkout.is_dir()
        and current.get("source_checkout_digest")
        and current.get("source_closure_digest")
        and current.get("source_inventory_identity", "") == inventory_identity
        and all(current.get(key) == expected.get(key) for key in attachable_keys)
        and all(ready.get(key) == current.get(key) for key in (
            *attachable_keys, "source_checkout", "source_checkout_digest",
            "source_closure_digest", "source_inventory_identity",
            "boot_manifest_path", "boot_manifest_hash", "python_executable",
        ))
        and ready.get("status") == "ready"
        and str(ready.get("pid")) == str(current.get("pid"))
        and str(ready.get("process_birth_id")) == str(current.get("process_birth_id"))
    ):
        return None
    resident_token = ""
    try:
        from astrid.core.execution.generic_host import (
            RuntimeProtocolClient,
            source_checkout_closure_digest,
            source_checkout_digest,
        )

        if (source_checkout_digest(checkout) != current["source_checkout_digest"]
                or source_checkout_closure_digest(checkout) != current["source_closure_digest"]):
            return None
        from astrid.core._shared.boot_manifest import load_boot_manifest_hash

        manifest = Path(str(current.get("boot_manifest_path") or ""))
        if load_boot_manifest_hash(manifest, support_root=Path(str(current["support_root"]))) != current["boot_manifest_hash"]:
            return None
        resident_token = worker_path.read_text(encoding="utf-8").strip()
        resident_client = RuntimeProtocolClient(str(expected["endpoint"]), resident_token)
        resident_client._authenticate_worker(PACK_HOST_ACTOR)
        registration = resident_client.generated.get_executor(PACK_HOST_ACTOR)
        def field(name: str):
            return registration.get(name) if isinstance(registration, Mapping) else getattr(registration, name, None)
        ready_registration = ready.get("registration")
        def ready_field(name: str):
            return ready_registration.get(name) if isinstance(ready_registration, Mapping) else getattr(ready_registration, name, None)
        caps = field("capabilities") or ()
        if not (
            field("executor_id") == PACK_HOST_ACTOR
            and field("readiness") == "ready"
            and field("runtime_epoch") == runtime_epoch
            and isinstance(ready.get("source_epoch"), str)
            and bool(ready.get("source_epoch"))
            and field("source_epoch") == ready.get("source_epoch") == ready_field("source_epoch")
            and bool(field("source_digest"))
            and field("source_digest") == ready_field("source_digest")
            and bool(field("dependency_digest"))
            and field("dependency_digest") == ready_field("dependency_digest")
            and any(
                (cap.get("status") if isinstance(cap, Mapping) else getattr(cap, "status", None)) == "ready"
                for cap in caps
            )
        ):
            return None
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
        return None
    finally:
        resident_token = ""
    return {
        "host_status": "ready",
        "host_pid": int(current["pid"]),
        "host_executor_id": PACK_HOST_ACTOR,
        "host_ready_file": str(expected["ready_file"]),
        "host_ready_capabilities": list(ready.get("ready_capabilities", [])),
        "host_runtime_instance_id": runtime_instance_id,
        "host_runtime_epoch": runtime_epoch,
        "host_source_checkout_digest": current["source_checkout_digest"],
        "host_source_closure_digest": current["source_closure_digest"],
        "host_source_inventory_identity": current.get("source_inventory_identity", ""),
        "host_boot_manifest_path": current["boot_manifest_path"],
        "host_boot_manifest_hash": current["boot_manifest_hash"],
    }


def ensure_pack_host(
    value: Mapping[str, Any], *, reconfigure_action: str,
    startup_idle_seconds: float = HOST_STARTUP_IDLE_SECONDS,
    attach_healthy: bool = False,
    _foreground: bool = False,
) -> Mapping[str, Any]:
    """Ensure the runtime-issued host is ready, waiting while it makes progress.

    There is no total-time cap: a source host registers in ~18 s on an idle
    8 GB Mac but takes minutes under heavy load.  The idle deadline resets on
    every progress signal (see _host_progress_token); the wait ends at once on
    host exit (with its log tail) or terminal registration failure, and fails
    as hung only after ``startup_idle_seconds`` with no progress.
    """
    # Reject invalid budgets before credential reads, discovery or any launch.
    if (isinstance(startup_idle_seconds, bool)
            or not isinstance(startup_idle_seconds, Real)
            or not math.isfinite(startup_idle_seconds)
            or startup_idle_seconds <= 0):
        raise ValueError("pack-host startup idle window must be finite, positive")
    startup_idle_seconds = float(startup_idle_seconds)
    worker_file = value.get("worker_credential_file")
    source_checkout = value.get("source_checkout")
    if not worker_file or not source_checkout:
        # Tiny fake launcher boundaries intentionally model only the runtime
        # handoff.  Real neutral-runtime results contain both fields.
        return {}
    from astrid.sdk.workspace_client import _safe_local_path

    try:
        worker_path = _safe_local_path(str(worker_file), field="worker credential")
        source_path = _safe_local_path(str(source_checkout), field="source checkout")
    except Exception as exc:
        raise PackHostBootstrapError(f"generic Astrid pack host handoff is unsafe; {reconfigure_action}") from exc
    if (not worker_path.is_file() or worker_path.is_symlink()
            or not source_path.is_dir() or source_path.is_symlink()
            or not platform.is_owner_only(worker_path)):
        raise PackHostBootstrapError(f"generic Astrid pack host handoff is unavailable; {reconfigure_action}")
    host_python = _pack_host_python_executable()
    if _foreground and host_python != _venv_interpreter_name(os.path.abspath(sys.executable)):
        raise PackHostBootstrapError("foreground pack host must run with its selected Python interpreter")
    execution_origins = _installed_execution_origins(source_path, host_python) if _execution_profile() == "installed" else {}
    try:
        from astrid.core.pack.source_setup import active_source_inventory

        managed_inventory = active_source_inventory()
    except Exception as exc:
        raise PackHostBootstrapError(
            f"managed source inventory could not be verified; {reconfigure_action}"
        ) from exc
    inventory_identity = managed_inventory.identity if managed_inventory.sources else ""
    pack_root = source_path / "astrid" / "packs"
    if not pack_root.is_dir() or pack_root.is_symlink():
        raise PackHostBootstrapError(f"Astrid source checkout has no pack root; {reconfigure_action}")
    scopes = tuple(str(scope) for scope in (value.get("worker_scopes") or ()))
    if str(value.get("worker_actor")) != PACK_HOST_ACTOR or scopes != PACK_HOST_SCOPES:
        raise PackHostBootstrapError(f"runtime worker credential is not the least-privilege pack-host contract; {reconfigure_action}")

    # Bind the process to the exact source and runtime instance it registered
    # against.  The health read is intentionally performed with the worker
    # credential, never the owner credential or an ambient environment token.
    from astrid.core.execution.generic_host import (
        RuntimeProtocolClient,
        source_checkout_closure_digest,
        source_checkout_digest,
    )

    try:
        source_digest = source_checkout_digest(source_path)
        source_closure_digest = source_checkout_closure_digest(source_path)
    except (OSError, ValueError) as exc:
        raise PackHostBootstrapError(
            f"generic Astrid pack source tree is not a safe checkout; {reconfigure_action}"
        ) from exc
    try:
        worker_token = worker_path.read_text(encoding="utf-8").strip()
        if not worker_token:
            raise ValueError("worker credential is empty")
        runtime_client = RuntimeProtocolClient(str(value["endpoint"]).rstrip("/"), worker_token)
        health = runtime_client.health()
    except Exception as exc:
        raise PackHostBootstrapError(
            f"generic Astrid pack host could not verify runtime identity; {reconfigure_action}"
        ) from exc
    finally:
        worker_token = ""
    health_value = dict(health) if isinstance(health, Mapping) else {
        "status": getattr(health, "status", None),
        "runtime_epoch": getattr(health, "runtime_epoch", None),
        "schema_digest": getattr(health, "schema_digest", None),
        "runtime_instance_id": getattr(health, "runtime_instance_id", None),
        "coordinator_epoch": getattr(health, "coordinator_epoch", None),
    }
    if str(health_value.get("status", "")) != "ok":
        raise PackHostBootstrapError(
            f"generic Astrid pack host runtime is not healthy; {reconfigure_action}",
            code="runtime_not_ready",
            terminal=True,
        )
    runtime_epoch = health_value.get("runtime_epoch", value.get("runtime_epoch"))
    runtime_instance_id = (
        value.get("runtime_instance_id")
        or health_value.get("runtime_instance_id")
        or value.get("coordinator_epoch")
        or health_value.get("coordinator_epoch")
        or (f"epoch:{runtime_epoch}" if runtime_epoch is not None else None)
    )
    schema_digest = health_value.get("schema_digest") or value.get("schema_digest")
    if runtime_epoch is None or runtime_instance_id is None:
        raise PackHostBootstrapError(
            f"generic Astrid pack host runtime identity is incomplete; {reconfigure_action}"
        )

    runtime_support = worker_path.parent.parent
    host_root = runtime_support / "astrid-host"
    boot_manifest_path = host_root / "boot-manifest.json"
    state_path = runtime_support / "generic-host.json"
    ready_path = runtime_support / "generic-host.ready.json"
    lock_path = runtime_support / "generic-host.lock"
    endpoint = str(value["endpoint"]).rstrip("/")
    try:
        lock_handle = lock_path.open("a+")
        platform.owner_only(lock_handle.fileno())
    except OSError as exc:
        raise PackHostBootstrapError(f"generic Astrid pack host lock is unavailable; {reconfigure_action}") from exc
    try:
        try:
            platform.lock_file(lock_handle.fileno())
        except (ImportError, OSError) as exc:
            raise PackHostBootstrapError(f"generic Astrid pack host lock is unavailable; {reconfigure_action}") from exc
        current = _read_object(state_path)
        ready = _read_object(ready_path)
        status = _registered_host_status(current) if current else None
        if not _foreground and attach_healthy and current and ready and status == "ours":
            receipt = _attach_resident_host(
                current, ready,
                expected={
                    "endpoint": endpoint,
                    "executor_id": PACK_HOST_ACTOR,
                    "ready_file": str(ready_path),
                    "credential_file": str(worker_path),
                    "support_root": str(runtime_support),
                    "runtime_instance_id": str(runtime_instance_id),
                    "runtime_epoch": runtime_epoch,
                    "schema_digest": schema_digest,
                },
                worker_path=worker_path,
                runtime_epoch=runtime_epoch,
                runtime_instance_id=str(runtime_instance_id),
                inventory_identity=inventory_identity,
            )
            if receipt is not None:
                return receipt
        from astrid.core.gateway.dispatch import compose_profile_handoff
        try:
            boot_handoff = compose_profile_handoff(
                boot_manifest_path, support_root=runtime_support
            )
        except Exception as exc:
            raise PackHostBootstrapError(
                f"generic Astrid pack boot manifest could not be composed; {reconfigure_action}"
            ) from exc
        boot_manifest_hash = str(boot_handoff["sha256"])
        expected = {
            # Preserve the venv executable path: resolving its symlink would
            # collapse different dependency environments to the base Python.
            "python_executable": host_python,
            "endpoint": endpoint,
            "executor_id": PACK_HOST_ACTOR,
            "ready_file": str(ready_path),
            "credential_file": str(worker_path),
            "support_root": str(runtime_support),
            "source_checkout": str(source_path),
            "source_checkout_digest": source_digest,
            "source_closure_digest": source_closure_digest,
            "source_inventory_identity": inventory_identity,
            "runtime_instance_id": str(runtime_instance_id),
            "runtime_epoch": runtime_epoch,
            "schema_digest": schema_digest,
            "boot_manifest_path": str(boot_manifest_path),
            "boot_manifest_hash": boot_manifest_hash,
        }
        if (not _foreground and current and ready
                and all(current.get(key) == expected_value for key, expected_value in expected.items())
                and status == "ours"
                and str(ready.get("status")) == "ready"
                and all(ready.get(key) == expected_value for key, expected_value in expected.items())
                and str(ready.get("pid")) == str(current.get("pid"))
                and str(ready.get("process_birth_id")) == str(current.get("process_birth_id"))):
            return {
                "host_status": "ready",
                "host_pid": int(current["pid"]),
                "host_executor_id": PACK_HOST_ACTOR,
                "host_ready_file": str(ready_path),
                "host_ready_capabilities": list(ready.get("ready_capabilities", [])),
                "host_runtime_instance_id": str(runtime_instance_id),
                "host_runtime_epoch": runtime_epoch,
                "host_source_checkout_digest": source_digest,
                "host_source_closure_digest": source_closure_digest,
                "host_source_inventory_identity": inventory_identity,
                "host_boot_manifest_path": str(boot_manifest_path),
                "host_boot_manifest_hash": boot_manifest_hash,
            }
        if current:
            # Any Astrid process may replace the host its registration names, but
            # reconfiguration is not cancellation: a host with child work is left
            # alone until that work finishes.  A dead host's registration (and any
            # orphans in its group) is simply retired.
            if status == "ours" and not _host_idle(int(current["pid"]), runtime_support):
                raise PackHostBootstrapError(
                    "generic Astrid pack host is busy with active child processes; "
                    "reconfiguration is deferred until its work finishes"
                )
            if _foreground:
                _retire_host_locked(current, foreground=True)
            else:
                _retire_host_locked(current)
        ready_path.unlink(missing_ok=True)
        log_path = runtime_support / "generic-host.log"
        try:
            # Each host start begins on a fresh, size-capped log.
            rotate_if_needed(log_path)
        except OSError:
            pass
        matrix = _capability_matrix_path(source_path)
        argv = [
            host_python,
            "-X", "utf8",
            *(["-I"] if execution_origins else []),
            "-m", "astrid.core.execution.generic_host", "run",
            "--pack-root", str(pack_root),
            "--runtime-endpoint", endpoint,
            "--runtime-discovery-file", str(runtime_support / "discovery.json"),
            "--credential-file", str(worker_path),
            "--executor-id", PACK_HOST_ACTOR,
            "--ready-file", str(ready_path),
            "--support-root", str(runtime_support),
            "--source-checkout", str(source_path),
            "--source-checkout-digest", source_digest,
            "--source-closure-digest", source_closure_digest,
            "--runtime-instance-id", str(runtime_instance_id),
            "--register",
            "--source-inventory-identity", inventory_identity,
            "--boot-manifest-path", str(boot_manifest_path),
            "--boot-manifest-hash", boot_manifest_hash,
        ]
        for managed_root in managed_inventory.roots:
            argv.extend(("--pack-root", str(managed_root)))
        if matrix.is_file():
            argv.extend(("--capability-matrix", str(matrix)))
        child_env = _host_render_environment(source_path)
        _verify_remotion_probe_custody(child_env)
        if _foreground:
            process_state = {
                "execution_profile": _execution_profile(), "execution_origins": execution_origins,
                **expected,
                "version": 2,
                "pid": os.getpid(),
                "process_birth_id": _host_birth_identity(os.getpid()),
            }
            if service_launch_id := _runtime_service_launch_id(foreground=True):
                process_state["runtime_service_launch_id"] = service_launch_id
            if not process_state["process_birth_id"]:
                raise PackHostBootstrapError("foreground pack host identity could not be captured")
            if sys.platform == "win32":
                image = _host_process_executable(os.getpid())
                if not image:
                    raise PackHostBootstrapError("foreground pack host executable identity is unavailable")
                process_state["process_image"] = image
            _write_object(state_path, process_state)
            # Unwind the bootstrap lock before entering the worker loop. The
            # Runtime owns this process, including its eventual exit/restart.
            raise _ForegroundHost(argv, child_env, runtime_support if execution_origins else source_path)
        try:
            log = log_path.open("ab")
            process = popen_owned_group(
                argv,
                cwd=str(runtime_support if execution_origins else source_path),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                detached=True,
                close_fds=True,
            )
        except OSError as exc:
            raise PackHostBootstrapError(f"generic Astrid pack host could not be started; {reconfigure_action}") from exc
        finally:
            try:
                log.close()
            except UnboundLocalError:
                pass
        process_state = {
            "execution_profile": _execution_profile(), "execution_origins": execution_origins,
            **expected,
            "version": 2,
            "pid": process.pid,
            "process_birth_id": _host_birth_identity(process.pid),
        }
        if not process_state["process_birth_id"]:
            if sys.platform == "win32":
                try:
                    terminate_group(process)
                except BaseException as cleanup:
                    raise PackHostBootstrapError(
                        f"generic Astrid pack host identity unavailable; cleanup is unproved; {reconfigure_action}"
                    ) from cleanup
            process.poll()
            raise PackHostBootstrapError(f"generic Astrid pack host identity could not be captured; {reconfigure_action}")
        if sys.platform == "win32":
            try:
                image = _host_process_executable(process.pid)
                if not image:
                    raise PackHostBootstrapError("host executable identity is unavailable")
            except BaseException as primary:
                try:
                    terminate_group(process)
                except BaseException as cleanup:
                    raise primary from cleanup
                raise
            process_state["process_image"] = image
            _RETAINED_HOSTS[(process.pid, process_state["process_birth_id"])] = process
        # Register at once: if this CLI dies before the host is ready, the next
        # Astrid process finds the registration and retires the orphan.
        _write_object(state_path, process_state)

        def _abandon() -> None:
            try:
                _retire_host_locked(process_state)
            finally:
                process.poll()

        now = time.monotonic()
        deadline = now + startup_idle_seconds
        next_probe = now
        last_token = None
        ready = None
        terminal_failure = None
        exit_status = None
        hung = False
        try:
            while True:
                now = time.monotonic()
                if now >= next_probe:
                    token = _host_progress_token(process, log_path, ready_path)
                    if token != last_token:
                        last_token = token
                        deadline = now + startup_idle_seconds
                    next_probe = now + _PROGRESS_PROBE_INTERVAL_SECONDS
                if now >= deadline:
                    hung = True
                    break
                ready = _read_object(ready_path)
                if ready and str(ready.get("status")) == "ready":
                    break
                if (
                    ready
                    and str(ready.get("status")) == "failed"
                    and ready.get("terminal") is True
                    and str(ready.get("pid")) == str(process.pid)
                    and str(ready.get("process_birth_id"))
                    == str(process_state["process_birth_id"])
                ):
                    terminal_failure = ready
                    break
                exit_status = process.poll()
                if exit_status is not None:
                    break
                time.sleep(0.05)
        except BaseException:
            _abandon()
            raise
        if terminal_failure is not None:
            _abandon()
            error = terminal_failure.get("error")
            diagnostic = error if isinstance(error, Mapping) else {}
            code = str(diagnostic.get("code") or "host_registration_failed")[:128]
            request_id = str(diagnostic.get("request_id") or "")[:128]
            cause = _host_failure_cause(log_path)
            message = cause or f"The task host couldn't register with Astrid Runtime; see {log_path}"
            suffix = f" [request_id={request_id}]" if request_id else ""
            raise PackHostBootstrapError(
                message + suffix,
                code=code,
                request_id=request_id,
                terminal=True,
            )
        if (not ready or ready.get("status") != "ready"
                or str(ready.get("pid")) != str(process.pid)
                or str(ready.get("process_birth_id")) != str(process_state["process_birth_id"])
                or not all(ready.get(key) == expected_value for key, expected_value in expected.items())
                or process.poll() is not None):
            _abandon()
            cause = _host_failure_cause(log_path)
            detail = f": {cause}" if cause else ""
            if hung:
                reason = (f"generic Astrid pack host is hung: no progress for "
                          f"{startup_idle_seconds:g}s while {_host_start_phase(log_path)}")
            elif exit_status is not None or process.poll() is not None:
                code = exit_status if exit_status is not None else process.poll()
                reason = f"generic Astrid pack host exited with status {code} during startup"
            else:
                reason = "generic Astrid pack host did not become ready"
            raise PackHostBootstrapError(f"{reason}{detail}; inspect {log_path}")
        return {
            "host_status": "ready",
            "host_pid": process.pid,
            "host_executor_id": PACK_HOST_ACTOR,
            "host_ready_file": str(ready_path),
            "host_ready_capabilities": list(ready.get("ready_capabilities", [])),
            "host_runtime_instance_id": str(runtime_instance_id),
            "host_runtime_epoch": runtime_epoch,
            "host_source_checkout_digest": source_digest,
            "host_source_closure_digest": source_closure_digest,
            "host_source_inventory_identity": inventory_identity,
            "host_boot_manifest_path": str(boot_manifest_path),
            "host_boot_manifest_hash": boot_manifest_hash,
        }
    finally:
        try:
            platform.unlock_file(lock_handle.fileno())
        except (ImportError, OSError):
            pass
        lock_handle.close()


class _ForegroundHost(Exception):
    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path):
        self.argv, self.env, self.cwd = argv, env, cwd


def run_pack_host_foreground(value: Mapping[str, Any]) -> int:
    """Run the ordinary pack host in the Runtime-supervised process."""
    try:
        ensure_pack_host(value, reconfigure_action="restart Astrid", _foreground=True)
    except _ForegroundHost as launch:
        from astrid.core.execution.generic_host import _cli

        os.chdir(launch.cwd)
        os.environ.clear()
        os.environ.update(launch.env)
        os.environ["ASTRID_PACK_HOST_SERVICE_MODE"] = "1"
        for path in reversed(launch.env.get("PYTHONPATH", "").split(os.pathsep)):
            if path and path not in sys.path:
                sys.path.insert(0, path)
        sys.argv = launch.argv[launch.argv.index("astrid.core.execution.generic_host"):]
        return _cli()
    raise PackHostBootstrapError("foreground pack host requires a complete Runtime worker handoff")


__all__ = ["PACK_HOST_ACTOR", "PACK_HOST_SCOPES", "PackHostBootstrapError", "ensure_pack_host", "run_pack_host_foreground"]
