"""Synchronous command transport for rendering protocol v1.

The result file is the only wire response.  Standard output and standard error
are retained as redacted diagnostics and are never parsed as protocol data.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, TypeAlias

from astrid.core.env_vars import ASTRID_INTERNAL_INVOCATION
from astrid.core.execution.process_group import (
    observe_tree,
    popen_owned_process,
    terminate_group,
    terminate_tree,
    verify_tree_absent,
)
from astrid.core.subprocess_env import build_child_subprocess_env

from .contracts import RendererError, RenderPlan, RenderResult, SupportReport
from .errors import (
    RendererException,
    make_renderer_error,
    raise_binary_missing_error,
    raise_internal_error,
    raise_invalid_artifact_error,
    raise_protocol_error,
    raise_renderer_error,
    raise_structured_failure,
    raise_timeout_error,
)

CommandVerb: TypeAlias = Literal["render", "support", "plan", "finalize"]
CommandResult: TypeAlias = RenderResult | SupportReport | RenderPlan

_VERBS = frozenset({"render", "support", "plan", "finalize"})
_QUALIFIED_ID_RE = re.compile(
    r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$"
)
_SECRET_NAME_RE = re.compile(
    r"(^|_)(API[_-]?KEY|AUTH|CREDENTIAL|PASSWORD|SECRET|TOKEN)($|_)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:api[_-]?key|auth(?:orization)?|credential|password|secret|token)"
    r"\s*[:=]\s*)(?:[^\s,;]+)"
)
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:sig|signature|token|secret|access_token|api_key|apikey|key)=)"
    r"[^&#\s]+"
)
_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"hf_[A-Za-z0-9]{12,}|AIza[0-9A-Za-z_-]{12,})"
)
_AUTH_HEADER_RE = re.compile(
    r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)"
    r"\s*:\s*[^\r\n]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_MAX_LOG_CHARS = 64 * 1024
_DEFAULT_TERMINATION_GRACE = 0.5


class CommandTransport:
    """Run one rendering protocol command in an owned process session.

    A backend may be bound when the transport is constructed or supplied to
    :meth:`run`.  ``env`` is an overlay on the host environment *before* the
    canonical child-environment allowlist is applied; it is not an escape hatch
    for passing arbitrary host variables.
    """

    def __init__(
        self,
        backend: str | None = None,
        *,
        termination_grace: float = _DEFAULT_TERMINATION_GRACE,
    ) -> None:
        if backend is not None:
            _validate_backend(backend)
        if (
            isinstance(termination_grace, bool)
            or not isinstance(termination_grace, (int, float))
            or not math.isfinite(float(termination_grace))
            or termination_grace <= 0
        ):
            raise ValueError("termination_grace must be a positive finite number")
        self.backend = backend
        self.termination_grace = float(termination_grace)
        self.last_logs: dict[str, str] = {"stdout": "", "stderr": ""}

    def run(
        self,
        verb: CommandVerb | str,
        command: Sequence[str | os.PathLike[str]],
        *,
        backend: str | None = None,
        request_path: str | os.PathLike[str],
        result_path: str | os.PathLike[str],
        cwd: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        required_binaries: Sequence[str | os.PathLike[str]] = (),
    ) -> CommandResult:
        """Execute a v1 verb and return its validated success DTO.

        Failures raise the matching ``RendererException`` subtype. A real
        ``KeyboardInterrupt`` after a successful ownership census is re-raised
        after the owned child tree has been terminated and the direct child
        reaped; the exception carries ``renderer_error``/``error`` attributes
        with the structured ``kind="interrupted"`` payload. If the census
        itself is interrupted, process ownership becomes uncertain and cleanup
        fails closed rather than signal an unverified tree.
        """

        selected_backend = backend or self.backend
        if selected_backend is None:
            raise ValueError("a qualified backend id is required")
        _validate_backend(selected_backend)
        self.last_logs = {"stdout": "", "stderr": ""}

        if verb not in _VERBS:
            raise_protocol_error(
                backend=selected_backend,
                message=f"unsupported rendering protocol verb {verb!r}",
                details={"received": verb, "supported": sorted(_VERBS)},
            )
        normalized_timeout = _validate_timeout(timeout, backend=selected_backend)
        argv_prefix = _normalize_command(command, backend=selected_backend)
        # Source-pack manifests deliberately use a portable bare ``python3``
        # token.  Resolve that generic token to the interpreter that owns this
        # Astrid runtime instead of consulting the sanitized child PATH.  A
        # PATH-selected interpreter can have a different ABI while PYTHONPATH
        # still points at this runtime's site-packages (for example CPython
        # 3.14 trying to import a CPython 3.11 native wheel).  Explicit paths
        # and version-qualified commands remain caller-controlled.
        if argv_prefix[0].casefold() in {"python", "python.exe", "python3", "python3.exe"}:
            argv_prefix[0] = sys.executable
        cwd_path = _resolve_cwd(cwd, backend=selected_backend)
        request = _absolute_path(request_path)
        result = _absolute_path(result_path)
        if request == result:
            raise_protocol_error(
                backend=selected_backend,
                message="request and result paths must be different",
                details={"path": str(request)},
            )

        child_env = _build_environment(env)
        from astrid.core.rendering._schema_observation import FD_ENV, RUN_ENV, observation_context

        child_env.pop(FD_ENV, None)
        child_env.pop(RUN_ENV, None)
        observation_fds: tuple[int, ...] = ()
        if verb == "render":
            observation = observation_context(os.environ.get(RUN_ENV), os.environ.get(FD_ENV))
            if observation is not None:
                child_env[RUN_ENV], fd = observation
                child_env[FD_ENV] = str(fd)
                observation_fds = (fd,)
        # Progress remains a host-owned retained-attempt file. Request data
        # and arbitrary environment overlays cannot nominate another path.
        inherited_progress = os.environ.get("ASTRID_PROGRESS_PATH")
        if inherited_progress and os.environ.get(ASTRID_INTERNAL_INVOCATION) == "1":
            progress = Path(inherited_progress)
            if (not progress.is_absolute() or progress.name != ".astrid-progress.json"
                    or progress.is_symlink() or not progress.parent.is_dir()
                    or any(part.is_symlink() for part in (progress.parent, *progress.parent.parents))
                    or not request.resolve().is_relative_to(progress.parent.resolve())
                    or not result.resolve().is_relative_to(progress.parent.resolve())):
                raise_protocol_error(backend=selected_backend,
                                     message="renderer progress path is outside the retained attempt")
            child_env["ASTRID_PROGRESS_PATH"] = str(progress)
        # The pack-root launcher routes among sibling manifest commands by the
        # transport-selected qualified backend id; empty backend_config in a
        # request must never make it guess from timeline shape.
        child_env["ASTRID_RENDER_BACKEND"] = selected_backend
        argv_prefix[0] = _resolve_executable(
            argv_prefix[0],
            cwd=cwd_path,
            child_env=child_env,
            backend=selected_backend,
        )
        for binary in required_binaries:
            binary_name = os.fspath(binary)
            _resolve_executable(
                binary_name,
                cwd=cwd_path,
                child_env=child_env,
                backend=selected_backend,
            )

        _remove_stale_result(result, backend=selected_backend)
        argv = [
            *argv_prefix,
            verb,
            "--request",
            str(request),
            "--result",
            str(result),
        ]
        secret_values = _secret_environment_values(os.environ, env)

        try:
            process = popen_owned_process(
                argv,
                shell=False,
                cwd=str(cwd_path),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=observation_fds,
                text=True,
                encoding="utf-8",
                errors="replace",
                # A serve-owned render child may establish the outer process
                # session as its containment boundary.  Keep the historical
                # detached-session default for all other callers.
                start_new_session=(
                    os.environ.get("ASTRID_RENDER_INHERIT_PROCESS_GROUP") != "1"
                ),
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise_binary_missing_error(
                backend=selected_backend,
                message=f"renderer executable is unavailable: {argv_prefix[0]}",
                details={
                    "binary": argv_prefix[0],
                    "error_type": type(exc).__name__,
                    **self.last_logs,
                },
            )
        except OSError as exc:
            raise_internal_error(
                backend=selected_backend,
                message=f"failed to start renderer command: {exc}",
                details={"error_type": type(exc).__name__, **self.last_logs},
            )
        except BaseException as exc:
            _release_failed_launch(exc)
            raise

        # Only a fresh session owns a group. Inherited mode must never signal
        # the host/sibling group; ancestry-fenced individual cleanup works in
        # both modes and also covers Remotion's detached browser session.
        if os.environ.get("ASTRID_RENDER_INHERIT_PROCESS_GROUP") != "1":
            process._astrid_process_group_id = process.pid
        try:
            deadline = (
                None
                if normalized_timeout is None
                else time.monotonic() + normalized_timeout
            )
            while True:
                if deadline is not None and normalized_timeout is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, normalized_timeout)
                # Finish custody's bounded observation even if the renderer's
                # execution allowance expires during its census. That expiry
                # requires normal timeout cleanup, not uncertain custody.
                observe_tree(process, deadline=None)
                remaining = (
                    None if deadline is None else deadline - time.monotonic()
                )
                if remaining is not None and normalized_timeout is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, normalized_timeout)
                try:
                    stdout, stderr = process.communicate(
                        timeout=(
                            0.05
                            if remaining is None
                            else min(0.05, remaining)
                        )
                    )
                    break
                except subprocess.TimeoutExpired:
                    if deadline is not None and normalized_timeout is not None and time.monotonic() >= deadline:
                        raise
        except subprocess.TimeoutExpired:
            stdout, stderr = _terminate_process_group(
                process, grace=self.termination_grace
            )
            logs = _redacted_logs(stdout, stderr, secret_values=secret_values)
            self.last_logs = logs
            raise_timeout_error(
                backend=selected_backend,
                message=f"renderer command timed out after {normalized_timeout:g} seconds",
                details={
                    "timeout_seconds": normalized_timeout,
                    "returncode": process.returncode,
                    **logs,
                },
            )
        except KeyboardInterrupt as exc:
            stdout, stderr = _terminate_process_group(
                process, grace=self.termination_grace
            )
            logs = _redacted_logs(stdout, stderr, secret_values=secret_values)
            self.last_logs = logs
            error = make_renderer_error(
                "interrupted",
                backend=selected_backend,
                message="renderer command was interrupted",
                details={"returncode": process.returncode, **logs},
            )
            # Preserve normal SIGINT/exit-130 behavior while still making the
            # frozen structured error available to an embedding caller.
            exc.renderer_error = error  # type: ignore[attr-defined]
            exc.error = error  # type: ignore[attr-defined]
            raise
        except Exception:
            # Any other post-spawn failure (including a defect in result
            # parsing) must still terminate and reap the process group so no
            # orphan is left behind.
            try:
                _terminate_process_group(process, grace=self.termination_grace)
            except Exception:  # noqa: BLE001 - preserve the original renderer error
                pass
            raise

        logs = _redacted_logs(stdout, stderr, secret_values=secret_values)
        self.last_logs = logs
        _terminate_leftover_group(process, grace=self.termination_grace)

        if process.returncode != 0:
            raise_internal_error(
                backend=selected_backend,
                message=f"renderer command exited with status {process.returncode}",
                details={"returncode": process.returncode, **logs},
            )

        payload = _read_result_file(
            result,
            backend=selected_backend,
            logs=logs,
        )
        return _parse_result(
            verb,
            payload,
            backend=selected_backend,
            logs=logs,
        )


def _validate_backend(backend: str) -> None:
    if not isinstance(backend, str) or not _QUALIFIED_ID_RE.fullmatch(backend):
        raise ValueError(
            "backend must be a qualified id '<pack>.<name>' using lowercase "
            "letters, digits, hyphens, or underscores"
        )


def _validate_timeout(timeout: float | None, *, backend: str) -> float | None:
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise_protocol_error(
            backend=backend,
            message="renderer timeout must be a positive finite number or null",
            details={"received": repr(timeout)},
        )
    return float(timeout)


def _normalize_command(
    command: Sequence[str | os.PathLike[str]], *, backend: str
) -> list[str]:
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
        raise_protocol_error(
            backend=backend,
            message="renderer command must be a non-empty argv sequence",
        )
    argv: list[str] = []
    for index, value in enumerate(command):
        if not isinstance(value, (str, os.PathLike)):
            raise_protocol_error(
                backend=backend,
                message=f"renderer command argument {index} must be a path string",
                details={"argument_index": index},
            )
        item = os.fspath(value)
        if not item or "\x00" in item:
            raise_protocol_error(
                backend=backend,
                message=f"renderer command argument {index} must be non-empty and contain no NUL",
                details={"argument_index": index},
            )
        argv.append(item)
    if not argv:
        raise_protocol_error(
            backend=backend,
            message="renderer command must contain at least one argument",
        )
    return argv


def _resolve_cwd(cwd: str | os.PathLike[str], *, backend: str) -> Path:
    try:
        path = Path(cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise_internal_error(
            backend=backend,
            message=f"renderer pack root is unavailable: {cwd}",
            details={"cwd": os.fspath(cwd), "error_type": type(exc).__name__},
        )
    if not path.is_dir():
        raise_internal_error(
            backend=backend,
            message=f"renderer pack root is not a directory: {path}",
            details={"cwd": str(path)},
        )
    return path


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _build_environment(env: Mapping[str, str] | None) -> dict[str, str]:
    base = dict(os.environ)
    if env is not None:
        base.update({str(key): str(value) for key, value in env.items()})
    return build_child_subprocess_env(base=base, parent=base)


def _resolve_executable(
    executable: str,
    *,
    cwd: Path,
    child_env: Mapping[str, str],
    backend: str,
) -> str:
    path_like = os.sep in executable or (
        os.altsep is not None and os.altsep in executable
    )
    if path_like:
        raw = Path(executable).expanduser()
        if raw.is_absolute():
            # Preserve an absolute executable symlink.  Virtual environments
            # rely on launching ``<venv>/bin/python`` through that symlink so
            # Python discovers the venv's ``pyvenv.cfg`` and site-packages;
            # resolving it to the base interpreter silently drops the venv.
            candidate = Path(os.path.abspath(raw))
        else:
            candidate = (cwd / raw).resolve(strict=False)
            try:
                candidate.relative_to(cwd)
            except ValueError:
                raise_binary_missing_error(
                    backend=backend,
                    message=f"pack-relative renderer executable escapes its pack root: {executable}",
                    details={"binary": executable, "cwd": str(cwd)},
                )
        resolved = str(candidate) if _is_executable_file(candidate) else None
    elif _is_executable_file(cwd / executable):
        # Manifest commands commonly name a pack-owned entrypoint without a
        # leading ``./``.  Resolve it explicitly because sanitized PATH must
        # not implicitly contain the pack root.
        resolved = str((cwd / executable).resolve())
    else:
        resolved = shutil.which(
            executable,
            path=child_env.get("PATH", os.defpath),
        )

    if resolved is None:
        raise_binary_missing_error(
            backend=backend,
            message=f"required renderer executable was not found: {executable}",
            recovery_command=f"install {executable} and retry",
            details={"binary": executable, "cwd": str(cwd)},
        )
    return resolved


def _is_executable_file(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and os.access(path, os.X_OK)


def _remove_stale_result(result_path: Path, *, backend: str) -> None:
    if not os.path.lexists(result_path):
        return
    try:
        if result_path.is_dir() and not result_path.is_symlink():
            raise IsADirectoryError(str(result_path))
        result_path.unlink()
    except OSError as exc:
        raise_invalid_artifact_error(
            backend=backend,
            message=f"cannot prepare authoritative result path: {result_path}",
            details={
                "result_path": str(result_path),
                "error_type": type(exc).__name__,
            },
        )


def _close_pipes(process: Any) -> None:
    for stream in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
        if stream is not None:
            stream.close()


def _release_failed_launch(error: BaseException) -> None:
    """Free what a refused launch left open; the retained custody error stays as raised.

    The launcher exposes its child and custody on the exception. This transport
    never reads that child's pipes, and a pidfd the kernel reports as exited no
    longer guards anything.
    """
    _close_pipes(getattr(error, "process", None))
    custody = getattr(error, "custody", None)
    if custody is not None and hasattr(custody, "release_pidfds"):
        custody.release_pidfds(only_exited=True)


def _terminate_process_group(
    process: subprocess.Popen[str], *, grace: float
) -> tuple[str, str]:
    """Terminate only the remembered renderer tree, then its owned group."""
    try:
        terminate_tree(process, grace_seconds=grace)
        if hasattr(process, "_astrid_process_group_id"):
            terminate_group(process, grace_seconds=grace)
        verify_tree_absent(process)
        try:
            stdout, stderr = process.communicate(timeout=max(grace, 1.0))
        except (subprocess.TimeoutExpired, OSError):
            raise RuntimeError("renderer pipes did not close after owned cleanup")
        return stdout or "", stderr or ""
    finally:
        # communicate() closes these on success; a failed or uncertain cleanup
        # raises before it, and the caller never reads them again.
        _close_pipes(process)


def _terminate_leftover_group(
    process: subprocess.Popen[str], *, grace: float
) -> None:
    """Verify remembered descendants after an otherwise completed command."""
    _terminate_process_group(process, grace=grace)


def _secret_environment_values(
    host: Mapping[str, str], overlay: Mapping[str, str] | None
) -> tuple[str, ...]:
    values: set[str] = set()
    for source in (host, overlay or {}):
        for key, value in source.items():
            text = str(value)
            if _SECRET_NAME_RE.search(str(key)) and len(text) >= 4:
                values.add(text)
    return tuple(sorted(values, key=len, reverse=True))


def _redact_log(value: str, *, secret_values: Sequence[str]) -> str:
    redacted = value.replace("\x00", "\ufffd")
    for secret in secret_values:
        redacted = redacted.replace(secret, "[redacted]")
    redacted = _AUTH_HEADER_RE.sub(
        lambda match: f"{match.group(1)}: [redacted]", redacted
    )
    redacted = _BEARER_RE.sub("Bearer [redacted]", redacted)
    redacted = _SECRET_QUERY_RE.sub(
        lambda match: f"{match.group(1)}[redacted]", redacted
    )
    redacted = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}[redacted]", redacted
    )
    redacted = _SECRET_VALUE_RE.sub("[redacted]", redacted)
    if len(redacted) > _MAX_LOG_CHARS:
        redacted = redacted[:_MAX_LOG_CHARS] + "\n[truncated]"
    return redacted


def _redacted_logs(
    stdout: str,
    stderr: str,
    *,
    secret_values: Sequence[str],
) -> dict[str, str]:
    return {
        "stdout": _redact_log(stdout or "", secret_values=secret_values),
        "stderr": _redact_log(stderr or "", secret_values=secret_values),
    }


def _read_result_file(
    result_path: Path,
    *,
    backend: str,
    logs: Mapping[str, str],
) -> Any:
    try:
        result_stat = result_path.lstat()
    except FileNotFoundError:
        raise_protocol_error(
            backend=backend,
            message=f"renderer did not write its authoritative result file: {result_path}",
            details={"result_path": str(result_path), **logs},
        )
    except OSError as exc:
        raise_invalid_artifact_error(
            backend=backend,
            message=f"cannot inspect renderer result file: {result_path}",
            details={
                "result_path": str(result_path),
                "error_type": type(exc).__name__,
                **logs,
            },
        )
    if stat.S_ISLNK(result_stat.st_mode) or not stat.S_ISREG(result_stat.st_mode):
        raise_invalid_artifact_error(
            backend=backend,
            message=f"renderer result path is not a regular file: {result_path}",
            details={"result_path": str(result_path), **logs},
        )
    try:
        return json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise_protocol_error(
            backend=backend,
            message=f"renderer wrote malformed result JSON: {exc}",
            details={
                "result_path": str(result_path),
                "error_type": type(exc).__name__,
                **logs,
            },
        )
    except OSError as exc:
        raise_invalid_artifact_error(
            backend=backend,
            message=f"cannot read renderer result file: {result_path}",
            details={
                "result_path": str(result_path),
                "error_type": type(exc).__name__,
                **logs,
            },
        )


def _parse_result(
    verb: str,
    payload: Any,
    *,
    backend: str,
    logs: Mapping[str, str],
) -> CommandResult:
    if isinstance(payload, Mapping) and "kind" in payload:
        try:
            emitted_error = RendererError.from_dict(payload)
        except RendererException as exc:
            _raise_requalified(exc, backend=backend, logs=logs)
        if emitted_error.backend != backend:
            raise_protocol_error(
                backend=backend,
                message="renderer error result names a different backend",
                details={"reported_backend": emitted_error.backend, **logs},
            )
        raise_renderer_error(
            replace(
                emitted_error,
                details={**emitted_error.details, **logs},
            )
        )

    parser: Any
    if verb in {"render", "finalize"}:
        parser = RenderResult.from_dict
    elif verb == "support":
        parser = SupportReport.from_dict
    else:
        parser = RenderPlan.from_dict

    try:
        parsed = parser(payload)
    except RendererException as exc:
        _raise_requalified(exc, backend=backend, logs=logs)

    if isinstance(parsed, SupportReport) and parsed.backend != backend:
        raise_protocol_error(
            backend=backend,
            message="support report names a different backend",
            details={"reported_backend": parsed.backend, **logs},
        )
    if isinstance(parsed, RenderPlan) and parsed.planner.id != backend:
        raise_protocol_error(
            backend=backend,
            message="render plan names a different planner",
            details={"reported_backend": parsed.planner.id, **logs},
        )
    if isinstance(parsed, RenderResult):
        captured = [
            f"{stream}:\n{text}"
            for stream, text in logs.items()
            if text.strip()
        ]
        parsed = replace(
            parsed,
            logs=[
                _redact_log(log, secret_values=()) for log in parsed.logs
            ]
            + captured,
        )
    return parsed


def _raise_requalified(
    exc: RendererException,
    *,
    backend: str,
    logs: Mapping[str, str],
) -> None:
    error = exc.error
    details = dict(error.details)
    if error.backend != backend:
        details.setdefault("reported_backend", error.backend)
    details.update(logs)
    raise_structured_failure(
        error.kind,
        backend=backend,
        message=error.message,
        recovery_command=error.recovery_command,
        details=details,
    )


__all__ = ["CommandResult", "CommandTransport", "CommandVerb"]
