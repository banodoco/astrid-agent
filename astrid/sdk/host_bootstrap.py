"""Process boundary for Astrid's one generic pack executor host.

The neutral runtime owns data and credentials.  This module only starts the
generic executor, waits for its registration/preflight readiness record, and
keeps a small support-directory marker so an Astrid relaunch reuses one host.
"""

from __future__ import annotations

import hashlib
import json
import math
from numbers import Real
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from astrid.core.execution.process_group import (
    _process_snapshot,
    CleanupUncertainError,
    verify_tree_absent,
    popen_owned_group,
    terminate_group,
)
from astrid.core.execution.host_log import rotate_if_needed
from astrid.core.generation.vibecomfy_dependency import (
    VibeComfyDependencyError,
    dependency_pythonpath,
)

INSTALLED_HOST_STARTUP_TIMEOUT_SECONDS = 120.0

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
            f"{PACK_HOST_PYTHON_ENV} must be an absolute path to an executable file"
        )
    try:
        executable = candidate.is_file() and os.access(candidate, os.X_OK)
    except (OSError, ValueError):
        executable = False
    if not executable:
        raise PackHostBootstrapError(
            f"{PACK_HOST_PYTHON_ENV} must be an absolute path to an executable file"
        )
    return _venv_interpreter_name(os.path.abspath(str(candidate)))

def _host_pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if sys.platform == "darwin":
        from astrid.core.execution.custody_broker import ordinary_candidate_observation
        observation = ordinary_candidate_observation(value)
        if observation["outcome"] in {"ABSENT", "ZOMBIE"}:
            return False
        if observation["outcome"] != "LIVE":
            raise PackHostBootstrapError("host native state observation unavailable")
        return True
    try:
        os.kill(value, 0)
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


def _host_launch_argv_source(state: Mapping[str, Any]) -> tuple[str, ...]:
    """Recover the protected launch source, never a redacted diagnostic tail."""
    argv = state.get("launch_argv_source")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 256
            or any(not isinstance(value, str) or "\0" in value for value in argv)
            or sum(len(os.fsencode(value)) + 1 for value in argv) > 64 * 1024
            or not Path(argv[0]).is_absolute()
            or _launch_argv_digest(argv) != state.get("launch_argv_sha256")):
        raise PackHostBootstrapError("host launch source is invalid; retirement blocked")
    bindings = {
        "python_executable": None, "endpoint": "--runtime-endpoint",
        "executor_id": "--executor-id", "ready_file": "--ready-file",
        "credential_file": "--credential-file", "support_root": "--support-root",
        "source_checkout": "--source-checkout", "source_checkout_digest": "--source-checkout-digest",
        "source_closure_digest": "--source-closure-digest",
        "source_inventory_identity": "--source-inventory-identity",
        "runtime_instance_id": "--runtime-instance-id",
        "boot_manifest_path": "--boot-manifest-path", "boot_manifest_hash": "--boot-manifest-hash",
    }
    for field, option in bindings.items():
        value = state.get(field)
        if option is None:
            matched = value == argv[0]
        else:
            index = argv.index(option) if option in argv else -1
            matched = (argv.count(option) == 1 and 0 <= index < len(argv) - 1
                       and value == argv[index + 1])
        if not isinstance(value, str) or not matched:
            raise PackHostBootstrapError("host launch source binding differs; retirement blocked")
    return tuple(argv)


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
                or before["pid"] != state.get("pid") or before["uid"] != os.getuid()
                or _ps_lstart_birth(before["start_sec"]) != state.get("process_birth_id")
                or not isinstance(state.get("launch_native_identity"), Mapping)
                or any(state["launch_native_identity"].get(key) != before.get(key)
                       for key in ("pid", "uid", "pgid", "start_sec", "start_usec"))):
            raise PackHostBootstrapError("host native launch binding unavailable; retirement blocked")
        if metadata["argv_sha256"] != pin:
            # Framework Python may replace only argv[0]. Across interpreters,
            # recover the exact tail from the protected, digest-bound source.
            process = _RETAINED_HOSTS.get((pid, str(state["process_birth_id"])))
            known = getattr(process, "_astrid_host_launch_argv", None)
            if known is None and "launch_argv_source" in state:
                known = _host_launch_argv_source(state)
            if (not isinstance(known, tuple) or not known or _launch_argv_digest(known) != pin
                    or metadata.get("executable") != argv[0]
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
                capture_output=True, text=True, check=False, timeout=1.0,
            )
            return probe.stdout.strip()
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""


def _our_host(pid: Any) -> bool:
    if sys.platform == "darwin":
        observation = _darwin_host_command_observation(int(pid))
        argv = observation["metadata_first"]["argv"]
        module = "astrid.core.execution.generic_host"
        opaque = "<opaque:sha256:" + hashlib.sha256(os.fsencode(module)).hexdigest() + ">"
        return any(argv[index] == "-m" and argv[index + 1] in {module, opaque}
                   for index in range(len(argv) - 1))
    return _host_pid_alive(pid) and "astrid.core.execution.generic_host" in _host_command(pid)


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
            [str(selected), "-I", "-S", "-c", probe],
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
    try:
        info = _process_snapshot().get(value)
    except Exception:
        info = None
    return str(info.birth) if info is not None else ""


def _host_identity_matches(state: Mapping[str, Any]) -> bool:
    """Verify PID, birth, process group, and every launch binding."""
    pid = state.get("pid")
    if sys.platform == "darwin":
        try:
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
        if os.getpgid(int(pid)) != int(pid):
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


_EXCEPTION_LINE = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?[A-Za-z_]\w*(?:Error|Exception|Failure)\b.*?:\s*\S")


def _host_failure_cause(log_path: Path, *, max_chars: int = 400) -> str:
    """Return the last exception line the child wrote to its log, if any.

    The bootstrap used to report only "inspect <log>", which hid the actual
    HostError (for example a capability matrix mismatch). Reads at most the last
    64 KiB and never raises: a missing or unreadable log yields an empty string.
    """
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
        if stripped.startswith(("HostError:", "PackHostBootstrapError:")) or _EXCEPTION_LINE.match(stripped):
            return stripped[:max_chars]
    return ""


def _read_object(path: Path) -> Mapping[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
            if isinstance(value, Mapping) and "launch_argv_source" in value:
                opened, named = os.fstat(stream.fileno()), path.lstat()
                if (not stat.S_ISREG(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o600
                        or opened.st_uid != os.getuid() or opened.st_nlink != 1
                        or stat.S_ISLNK(named.st_mode)
                        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
                    raise PackHostBootstrapError("host launch source is unprotected; retirement blocked")
                _host_launch_argv_source(value)
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
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _dependency_pythonpath() -> tuple[str, ...]:
    """Keep only approved dependency roots across the host boundary."""
    try:
        return dependency_pythonpath()
    except VibeComfyDependencyError as exc:
        raise PackHostBootstrapError(str(exc)) from exc


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

        resolution = resolve_pinned_node(
            project_dir,
            override=(child_env.get(ASTRID_NODE_EXECUTABLE) or "").strip() or None,
            search_path=child_env.get("PATH"),
        )
        if not resolution.ok:
            raise PackHostBootstrapError(f"the pack host was not started: {resolution.problem}")
        child_env[ASTRID_NODE_EXECUTABLE] = str(resolution.path)
    elif ASTRID_NODE_EXECUTABLE not in child_env:
        node = shutil.which("node", path=child_env.get("PATH"))
        if node:
            child_env[ASTRID_NODE_EXECUTABLE] = str(Path(node).resolve())


def _descendant_snapshot(pid: int) -> dict[int, tuple[str, int]]:
    """Capture descendant birth/PGID pairs before stopping a host."""
    try:
        snapshot = _process_snapshot()
    except Exception:
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


_RETAINED_HOSTS: dict[tuple[int, str], Any] = {}


def _terminate_descendants(members: Mapping[int, tuple[str, int]]) -> None:
    """A numeric census carries no destructive cleanup authority."""
    if members:
        raise PackHostBootstrapError("prior descendant custody is unresolved; replacement blocked")


def _terminate_old_host(state: Mapping[str, Any]) -> None:
    """Stop a retained exact graph; stale marker possession grants no authority."""
    try:
        key = (int(state.get("pid")), str(state.get("process_birth_id") or ""))
    except (TypeError, ValueError):
        raise PackHostBootstrapError("prior host identity is invalid; replacement blocked")
    process = _RETAINED_HOSTS.get(key)
    if process is None:
        raise PackHostBootstrapError("prior host cleanup custody is unresolved; replacement blocked")
    try:
        terminate_group(process)
        verify_tree_absent(process)
    except CleanupUncertainError as exc:
        # A graph failure must not discard the original root capability. Root
        # cessation is separate evidence and cannot retire the graph or marker.
        try:
            from astrid.core.execution.custody_broker import ProcessCustody
            custody = getattr(process, "_astrid_custody", None)
            if (not isinstance(custody, ProcessCustody) or custody.process is not process
                    or custody._handles.get(key[0], (None,))[0] != key[1]):
                raise PackHostBootstrapError("original host root capability is unavailable")
            custody.signal(key[0], key[1], signal.SIGTERM)
            try:
                returncode = process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                custody.signal(key[0], key[1], signal.SIGKILL)
                returncode = process.wait(timeout=1.0)
            process._astrid_root_cessation = {
                "pid": key[0], "birth_id": key[1], "original_wait_returncode": returncode,
                "graph_cleanup": "UNRESOLVED",
            }
        except BaseException as root_error:
            process._astrid_root_cessation_error = type(root_error).__name__
        raise PackHostBootstrapError("prior host graph cleanup is uncertain; replacement blocked") from exc


def _retained_host_registration(state: Mapping[str, Any]):
    try:
        key = (int(state.get("pid")), str(state.get("process_birth_id") or ""))
    except (TypeError, ValueError) as exc:
        raise PackHostBootstrapError("prior host identity is invalid; retirement blocked") from exc
    process = _RETAINED_HOSTS.get(key)
    registration = getattr(process, "_astrid_host_registration", None)
    if process is None or registration is None:
        raise PackHostBootstrapError("prior host cleanup custody is unresolved; retirement blocked")
    if dict(state) != registration[2]:
        raise PackHostBootstrapError("prior host registration differs; retirement blocked")
    return process, registration


def _retire_owned_host_locked(state: Mapping[str, Any]) -> None:
    """Caller holds the existing host lock; never acquire it recursively."""
    process, (state_path, ready_path, expected) = _retained_host_registration(state)
    if getattr(process, "_astrid_host_registration_retired", False):
        return
    identity_fields = (
        "pid", "process_birth_id", "runtime_instance_id", "runtime_epoch",
        "boot_manifest_hash", "executor_id", "support_root", "ready_file",
        "source_checkout", "source_closure_digest", "schema_digest",
    )
    try:
        if state_path.is_symlink() or ready_path.is_symlink():
            raise PackHostBootstrapError("prior host registration is aliased; retirement blocked")
        state_bytes, ready_bytes = state_path.read_bytes(), ready_path.read_bytes()
        marker, ready = json.loads(state_bytes), json.loads(ready_bytes)
        if (marker != expected or not isinstance(ready, dict)
                or ready.get("status") != "ready"
                or any(ready.get(field) != expected.get(field) for field in identity_fields)):
            raise PackHostBootstrapError("prior host registration differs; retirement blocked")
        _terminate_old_host(state)
        process.wait(timeout=0)
        # Preserve the obligation if either publisher record changed while
        # the original owner was stopping its retained graph.
        if (state_path.is_symlink() or ready_path.is_symlink()
                or state_path.read_bytes() != state_bytes
                or ready_path.read_bytes() != ready_bytes):
            raise PackHostBootstrapError("prior host registration changed during cleanup; retirement blocked")
        ready_path.unlink()
        state_path.unlink()
        process._astrid_host_registration_retired = True
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise PackHostBootstrapError("prior host registration retirement is incomplete; replacement blocked") from exc


def _stop_owned_host(state: Mapping[str, Any]) -> None:
    """The original creator stops and retires its exact host registration."""
    process, (state_path, _, _) = _retained_host_registration(state)
    if getattr(process, "_astrid_host_registration_retired", False):
        return
    try:
        import fcntl
        with state_path.with_name("generic-host.lock").open("a+") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            _retire_owned_host_locked(state)
    except (ImportError, OSError) as exc:
        raise PackHostBootstrapError("prior host registration lock is unavailable; retirement blocked") from exc


def _execution_profile() -> str:
    profile = os.environ.get("ASTRID_HOST_EXECUTION_PROFILE", "source").strip()
    if profile not in {"source", "installed"}:
        raise PackHostBootstrapError("host execution profile must be source or installed")
    return profile


def _installed_execution_origins(source_path: Path, python_executable: str) -> dict[str, str]:
    """Probe the selected interpreter in isolation, without a checkout path.

    The profile must bind the installed product root as its execution source.
    An old provenance checkout is diagnostic data, never import authority.
    """
    probe = """import importlib.util,json
modules={'host':'astrid.core.execution.generic_host','child':'astrid.core.execution.generic_host_worker','action':'astrid.packs.rendering.actions.render.run','client':'banodoco_workspace_client','sdk':'astrid.sdk.invocation'}
print(json.dumps({key:importlib.util.find_spec(module).origin for key,module in modules.items()}))
"""
    completed = subprocess.run([python_executable, "-I", "-c", probe],
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


def _host_render_environment(source_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop('ASTRID_PACKS_PATH', None)
    if _execution_profile() == "installed":
        _installed_execution_origins(source_path, _pack_host_python_executable())
        environment.pop("PYTHONPATH", None)
        # Launching from an unrelated directory avoids Python's empty-path
        # source preference; installed dependencies must belong to the venv.
    else:
        environment['PYTHONPATH'] = os.pathsep.join((str(source_path), *_dependency_pythonpath()))
    _provision_render_runtime_env(source_path, environment)
    return environment


def _prepare_render_validation(source_path: Path, env: dict[str, str],
                               support_root: Path, python_executable: str,
                               pack_roots: tuple[Path, ...] = ()) -> str | None:
    from astrid.core.env_vars import (
        ASTRID_REMOTION_PROJECT_DIR, ASTRID_REMOTION_VALIDATION_PATH,
        ASTRID_REMOTION_VALIDATION_SHA256, ASTRID_REMOTION_VALIDATION_LAUNCH,
        ASTRID_REMOTION_VALIDATION_ROLE,
    )
    from astrid.core.rendering.remotion_runtime import prepare_remotion_validation
    from astrid.core.subprocess_env import build_child_subprocess_env

    for name in (ASTRID_REMOTION_VALIDATION_PATH, ASTRID_REMOTION_VALIDATION_SHA256,
                 ASTRID_REMOTION_VALIDATION_LAUNCH, ASTRID_REMOTION_VALIDATION_ROLE):
        env.pop(name, None)  # A new launch never imports another host's readiness.
    if not env.get(ASTRID_REMOTION_PROJECT_DIR):
        return None  # No provisioned Remotion: existing unavailable capability.
    launch_id = secrets.token_hex(16)
    # This is the canonical worker filter. Invocation metadata added later is
    # outside the explicitly adopted clean probe environment contract.
    if pack_roots:
        from astrid.core.execution.generic_host import _launch_worker_probe_environments
        matrix = source_path / 'config' / 'astrid-beta-capabilities.json'
        workers = _launch_worker_probe_environments(
            pack_roots, env, matrix if matrix.is_file() else None)
    else:
        workers = [build_child_subprocess_env(base=env, parent=env)]
    reference = prepare_remotion_validation(
        artifact_directory=Path(support_root) / 'remotion-validation',
        host_environment=env, python_executable=python_executable,
        launch_id=launch_id, contexts={'host': dict(env), 'worker': workers},
    )
    env.update(reference)
    return launch_id


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
        env = build_child_subprocess_env(parent={}, explicit_env=projected_env)
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
        validation_launch = _prepare_render_validation(
            reference.source_checkout, env, Path(reference.support_root), str(launch.argv[0]),
            tuple(Path(root) for root in reference.pack_roots))
        parent, child = socket.socketpair()
        operation_id, channel_id = secrets.token_hex(16), secrets.token_hex(16)
        launch_argv = list(launch.argv)
        if origins and "-I" not in launch_argv[1:]:
            launch_argv.insert(1, "-I")
        argv = [*launch_argv,
                "--source-closure-digest", reference.source_closure_digest.removeprefix("sha256:"),
                "--activation-fd", str(child.fileno()),
                "--activation-operation-id", operation_id,
                "--activation-channel-id", channel_id,
                "--activation-timeout-seconds", str(self.timeout_seconds)]
        if validation_launch is not None:
            argv.extend(("--remotion-validation-launch-id", validation_launch))
        log_path = Path(reference.support_root) / f"generic-host-{operation_id}.log"
        launch_digest = _launch_argv_digest(argv)
        try:
            with log_path.open("xb") as log:
                os.fchmod(log.fileno(), 0o600)
                process = popen_owned_group(
                    argv, cwd=str(reference.support_root if origins else launch.cwd), env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, close_fds=True, pass_fds=(child.fileno(),),
                )
        except Exception:
            parent.close()
            raise
        finally:
            child.close()
        process._astrid_host_launch_argv = tuple(argv)
        state = {
            "launch_argv_sha256": launch_digest,
            "pid": process.pid, "process_birth_id": _host_birth_identity(process.pid),
            "remotion_validation": {k: v for k, v in env.items() if k.startswith("ASTRID_REMOTION_VALIDATION_")} or None,
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
        if handle.grant_sent or handle.process.poll() is not None or not _host_identity_matches(handle.state):
            raise PackHostBootstrapError("parked host grant is consumed or its incarnation changed")
        if (set(grant) != {"activation_id", "credential_file", "executor_incarnation", "evidence_digest"}
                or grant["credential_file"] != handle.state["credential_file"]):
            raise PackHostBootstrapError("parked host grant is foreign")
        credential_path = Path(grant["credential_file"])
        if (credential_path.is_symlink() or not credential_path.is_file()
                or credential_path.stat().st_mode & 0o777 != 0o600):
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
                if received != request or not _host_identity_matches(handle.state):
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
                if (not _host_identity_matches(handle.state)
                        or ready.get("pid") != handle.process.pid
                        or ready.get("process_birth_id") != handle.state["process_birth_id"]
                        or ready.get("executor_id") != reference.executor_id
                        or any(ready.get(key) != value for key, value in handle.state.items()
                               if key not in {"pid", "process_birth_id"})):
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

        if not isinstance(handle, _ParkedPackHostHandle) or not _host_identity_matches(handle.state):
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
            uid = subprocess.run(["ps", "-p", str(handle.process.pid), "-o", "uid="],
                                 capture_output=True, text=True, check=True, timeout=1).stdout.strip()
        if (info is None or info.birth != handle.state["process_birth_id"]
                or not command or not _host_executable_matches(command[0], reference.executable.path)
                or int(uid) != os.getuid()):
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
                        "sid": os.getsid(info.pid), "uid": int(uid), "executable": command[0],
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
        if self.provider_observer is not None:
            provider = self.provider_observer(handle)
            target = reference.effective_target
            if provider != {"account_ref": target.get("provider_account_ref"), "pod_id": target.get("pod_id")}:
                raise PackHostBootstrapError("parked host provider identity changed")
            result["target"] = dict(target)
            result.pop("machine_identity")
            result["provider_identity"] = dict(provider)
            output = Path(reference.output_root)
            if output.is_symlink() or not output.is_dir():
                raise PackHostBootstrapError("parked host output root is unsafe")
            result["output_root"] = str(output.resolve(strict=True))
        return result

    def observe_ready(self, handle: object) -> Mapping[str, Any]:
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


def ensure_pack_host(
    value: Mapping[str, Any], *, reconfigure_action: str,
    startup_timeout_seconds: float = 20.0,
) -> Mapping[str, Any]:
    """Ensure the runtime-issued host is ready within an explicit bounded budget."""
    # Reject invalid budgets before credential reads, discovery or any launch.
    if (isinstance(startup_timeout_seconds, bool)
            or not isinstance(startup_timeout_seconds, Real)
            or not math.isfinite(startup_timeout_seconds)
            or not 0 < startup_timeout_seconds <= INSTALLED_HOST_STARTUP_TIMEOUT_SECONDS):
        raise ValueError("pack-host startup timeout must be finite, positive and at most 120 seconds")
    startup_timeout_seconds = float(startup_timeout_seconds)
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
            or worker_path.stat().st_mode & 0o777 != 0o600):
        raise PackHostBootstrapError(f"generic Astrid pack host handoff is unavailable; {reconfigure_action}")
    host_python = _pack_host_python_executable()
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
    state_path = runtime_support / "generic-host.json"
    ready_path = runtime_support / "generic-host.ready.json"
    lock_path = runtime_support / "generic-host.lock"
    endpoint = str(value["endpoint"]).rstrip("/")
    try:
        lock_handle = lock_path.open("a+")
        os.fchmod(lock_handle.fileno(), 0o600)
    except OSError as exc:
        raise PackHostBootstrapError(f"generic Astrid pack host lock is unavailable; {reconfigure_action}") from exc
    try:
        try:
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError) as exc:
            raise PackHostBootstrapError(f"generic Astrid pack host lock is unavailable; {reconfigure_action}") from exc
        current = _read_object(state_path)
        ready = _read_object(ready_path)
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
        if (current and ready
                and all(current.get(key) == expected_value for key, expected_value in expected.items())
                and _host_identity_matches(current)
                and str(ready.get("status")) == "ready"
                and all(ready.get(key) == expected_value for key, expected_value in expected.items())
                and str(ready.get("pid")) == str(current.get("pid"))
                and str(ready.get("process_birth_id")) == str(current.get("process_birth_id"))):
            reference = current.get("remotion_validation")
            reuse_env = _host_render_environment(source_path)
            if reuse_env.get("ASTRID_REMOTION_PROJECT_DIR") and reference is None:
                raise PackHostBootstrapError("prior Remotion host lacks launch validation; reconfigure explicitly")
            if reference is not None:
                from astrid.core.rendering.remotion_runtime import _validated_runtime
                from astrid.core.env_vars import ASTRID_REMOTION_PROJECT_DIR
                if ready.get("remotion_validation") != reference:
                    raise PackHostBootstrapError("prior Remotion launch validation marker differs")
                try:
                    _validated_runtime(Path(reuse_env[ASTRID_REMOTION_PROJECT_DIR]),
                                       environment={**reuse_env, **reference}, python_executable=host_python)
                except Exception as exc:
                    raise PackHostBootstrapError("prior Remotion launch validation is stale; reconfigure explicitly") from exc
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
            # Reconfiguration is not cancellation. In particular, a status
            # command from another interpreter or after a documentation edit
            # must not terminate a render already owned by this host.
            if _host_identity_matches(current) and _descendant_snapshot(int(current["pid"])):
                raise PackHostBootstrapError(
                    "generic Astrid pack host is busy with active child processes; "
                    "reconfiguration is deferred until its work finishes"
                )
            _retire_owned_host_locked(current)
        ready_path.unlink(missing_ok=True)
        log_path = runtime_support / "generic-host.log"
        try:
            # Each host start begins on a fresh, size-capped log.
            rotate_if_needed(log_path)
        except OSError:
            pass
        matrix = source_path / "config" / "astrid-beta-capabilities.json"
        argv = [
            host_python,
            *(["-I"] if execution_origins else []),
            "-m", "astrid.core.execution.generic_host", "run",
            "--pack-root", str(pack_root),
            "--runtime-endpoint", endpoint,
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
        validation_launch = _prepare_render_validation(
            source_path, child_env, runtime_support, host_python,
            (pack_root, *managed_inventory.roots))
        if validation_launch is not None:
            argv.extend(("--remotion-validation-launch-id", validation_launch))
        launch_digest = _launch_argv_digest(argv)
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
                close_fds=True,
            )
        except OSError as exc:
            raise PackHostBootstrapError(f"generic Astrid pack host could not be started; {reconfigure_action}") from exc
        finally:
            try:
                log.close()
            except UnboundLocalError:
                pass
        process._astrid_host_launch_argv = tuple(argv)
        _RETAINED_HOSTS[(process.pid, _host_birth_identity(process.pid))] = process
        process_state = {
            "launch_argv_sha256": launch_digest,
            "launch_argv_source": list(argv),
            "execution_profile": _execution_profile(), "execution_origins": execution_origins,
            **expected,
            "version": 2,
            "pid": process.pid,
            "process_birth_id": _host_birth_identity(process.pid),
            "remotion_validation": {k: v for k, v in child_env.items() if k.startswith("ASTRID_REMOTION_VALIDATION_")} or None,
        }
        try:
            process_state["launch_native_identity"] = _capture_native_host_launch_identity(process.pid)
        except BaseException as primary:
            try:
                _terminate_old_host(process_state)
            except BaseException as cleanup:
                raise primary from cleanup
            raise
        if not process_state["process_birth_id"]:
            _terminate_old_host(process_state)
            raise PackHostBootstrapError(f"generic Astrid pack host identity could not be captured; {reconfigure_action}")
        deadline = time.monotonic() + startup_timeout_seconds
        ready = None
        terminal_failure = None
        while time.monotonic() < deadline:
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
            if process.poll() is not None:
                break
            time.sleep(0.05)
        if terminal_failure is not None:
            _terminate_old_host(process_state)
            error = terminal_failure.get("error")
            diagnostic = error if isinstance(error, Mapping) else {}
            code = str(diagnostic.get("code") or "host_registration_failed")[:128]
            request_id = str(diagnostic.get("request_id") or "")[:128]
            message = str(
                diagnostic.get("message") or "generic Astrid pack host registration failed"
            )[:512]
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
            _terminate_old_host(process_state)
            cause = _host_failure_cause(log_path)
            detail = f": {cause}" if cause else ""
            raise PackHostBootstrapError(
                f"generic Astrid pack host did not become ready{detail}; inspect {log_path}"
            )
        _write_object(state_path, process_state)
        # Keep the paths and identity with the original process owner. A later
        # interpreter possessing these public markers cannot acquire custody.
        process._astrid_host_registration = (state_path, ready_path, dict(process_state))
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
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        lock_handle.close()


__all__ = ["PACK_HOST_ACTOR", "PACK_HOST_SCOPES", "PackHostBootstrapError", "ensure_pack_host"]
