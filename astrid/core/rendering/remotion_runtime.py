"""Server-owned Remotion runtime discovery for Astrid render admission.

The Python package does not contain a Node checkout.  A release deployment
must point ``ASTRID_REMOTION_PROJECT_DIR`` at its separately provisioned,
server-owned Remotion bundle.  No task/request value is ever consulted here.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from astrid.core.env_vars import (
    ASTRID_NODE_EXECUTABLE,
    ASTRID_REMOTION_PROJECT_DIR,
    ASTRID_TIMELINE_SCHEMA_PYTHONPATH,
)
from astrid.core.foundation.paths import REPO_ROOT

REMOTION_PROJECT_DIR_ENV = ASTRID_REMOTION_PROJECT_DIR
NODE_EXECUTABLE_ENV = ASTRID_NODE_EXECUTABLE
TIMELINE_SCHEMA_PYTHONPATH_ENV = ASTRID_TIMELINE_SCHEMA_PYTHONPATH
REMOTION_CLI_RELATIVE_PATH = Path("node_modules/@remotion/cli/remotion-cli.js")
_NODE_PROBE_TIMEOUT_SECONDS = 5
_REQUIRED_PACKAGES = (
    "timeline-composition",
    "timeline-schema",
    "timeline-theme-2rp",
)
_REQUIRED_SCHEMA_FILES = (
    "__init__.py",
    "generated.py",
    "materialize.py",
    "theme.py",
    "timeline.schema.json",
    "validate.py",
)

_SCHEMA_PROBE = """
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import banodoco_timeline_schema as schema
from banodoco_timeline_schema import (
    AssetEntry,
    Theme,
    ThemeOverrides,
    TimelineClip,
    TimelineConfig,
    TimelineOutput,
    materialize_output,
    resolve_theme,
)
origin = pathlib.Path(schema.__file__).resolve()
if not origin.is_relative_to(root / "banodoco_timeline_schema"):
    raise RuntimeError(f"schema origin escaped trusted root: {origin}")
contract = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
import jsonschema
jsonschema.validate(
    {"tracks": [{"id": "source", "kind": "visual", "label": "Source", "role": "source"}],
     "clips": []},
    contract,
)
print(origin)
"""


@dataclass(frozen=True, slots=True)
class RemotionRuntimeStatus:
    available: bool
    project_dir: Path | None
    reason: str | None = None
    node_executable: Path | None = None
    node_version: str | None = None
    remotion_cli: Path | None = None


@dataclass(frozen=True, slots=True)
class RemotionRuntimeTools:
    """Validated server-owned executables for one Remotion project."""

    node_executable: Path
    node_version: str
    remotion_cli: Path


class RemotionProbeCleanupError(RuntimeError):
    """A failed probe whose child process could not be proved settled."""

    process: subprocess.Popen[str]
    cleanup_error: BaseException


_UNRESOLVED_PROBE_CUSTODY: list[RemotionProbeCleanupError] = []


def _probe_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    from astrid.core.subprocess_env import _SAFE_BASE_ENV, _WINDOWS_BASE_ENV

    keys = _SAFE_BASE_ENV | (_WINDOWS_BASE_ENV if sys.platform == "win32" else frozenset())
    source = os.environ if environment is None else environment
    return {"PATH": os.defpath, **{key: value for key, value in source.items() if key in keys}}


def _owned_probe(argv: list[str], *, cwd: Path, env: Mapping[str, str],
                 timeout: float) -> tuple[subprocess.CompletedProcess, dict[str, Any]]:
    from astrid.core.execution.process_group import (
        popen_owned_group,
        terminate_group,
        verify_tree_absent,
    )

    # The existing launch primitive treats an empty mapping as ambient env.
    if not env:
        raise ValueError("owned probe needs an explicit nonempty environment")
    process = None
    try:
        process = popen_owned_group(
            argv, cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        stdout, stderr = process.communicate(timeout=timeout)
        code = process.wait(timeout=0)
        verify_tree_absent(process)
    except BaseException as primary:
        # A failed acquisition can expose its retained child on the exception.
        process = process or getattr(primary, "process", None)
        if process is not None:
            try:
                terminate_group(process, grace_seconds=0.2)
                process.wait(timeout=1)
                verify_tree_absent(process)
            except BaseException as cleanup:
                primary.process = process
                primary.cleanup_error = cleanup
                fatal = RemotionProbeCleanupError(
                    f"trusted Remotion probe cleanup is unproved: {primary}; cleanup failed: {cleanup}"
                )
                fatal.process = process
                fatal.cleanup_error = cleanup
                _UNRESOLVED_PROBE_CUSTODY.append(fatal)
                raise fatal from cleanup
        if process is not None:
            primary.probe_receipt = {
                "original_child_pid": process.pid,
                "original_wait_exit_code": process.returncode,
                "retained_set_final_census_complete": True,
                "custody_admitted": getattr(process, "_astrid_custody", None) is not None,
                "outcome": "FAILED_VALIDATION_SETTLED",
            }
        raise
    receipt = {
        "argv": argv, "cwd": str(cwd), "timeout_seconds": timeout,
        "original_child_pid": process.pid, "original_wait_exit_code": code,
        "custody_admitted": getattr(process, "_astrid_custody", None) is not None,
        "retained_set_final_census_complete": True,
        "complete_descendant_history": "UNPROVED",
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
    }
    return subprocess.CompletedProcess(argv, code, stdout, stderr), receipt

def _probe_schema(schema_root: Path, *, environment: Mapping[str, str] | None = None) -> str | None:
    """Import the installed schema in isolation and check its module origin."""
    try:
        probe, _receipt = _owned_probe(
            [sys.executable, "-X", "utf8", "-I", "-c", _SCHEMA_PROBE, str(schema_root),
             str(REPO_ROOT / "astrid/core/timeline/schema/timeline-config.schema.json")],
            cwd=schema_root.parent, env=_probe_environment(environment), timeout=_NODE_PROBE_TIMEOUT_SECONDS,
        )
        if probe.returncode:
            return f"clean timeline schema validation failed: {probe.stderr.strip()}"
        origin = Path(probe.stdout.strip())
        if not origin.is_absolute() or not origin.resolve().is_relative_to(schema_root / "banodoco_timeline_schema"):
            return f"timeline schema origin escaped trusted root: {origin}"
    except RemotionProbeCleanupError:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return f"clean timeline schema validation failed: {exc}"
    return None


def _configured_node_executable(
    environment: Mapping[str, str] | None = None,
) -> tuple[Path | None, str | None]:
    source = os.environ if environment is None else environment
    raw = source.get(NODE_EXECUTABLE_ENV, "").strip()
    if not raw:
        return None, f"{NODE_EXECUTABLE_ENV} must point to the server-owned Node executable"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return None, f"{NODE_EXECUTABLE_ENV} must be an absolute path"
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        return None, f"{NODE_EXECUTABLE_ENV} is not a readable file: {exc}"
    if not resolved.is_file():
        return None, f"{NODE_EXECUTABLE_ENV} is not a file: {resolved}"
    if not os.access(resolved, os.X_OK):
        return None, f"{NODE_EXECUTABLE_ENV} is not executable: {resolved}"
    return resolved, None


def _probe_node(
    node_executable: Path, *, cwd: Path, environment: Mapping[str, str] | None = None
) -> tuple[str | None, str | None]:
    """Bounded version probe for the trusted Node executable."""

    try:
        probe, _receipt = _owned_probe(
            [str(node_executable), "--version"], cwd=cwd,
            env=_probe_environment(environment), timeout=_NODE_PROBE_TIMEOUT_SECONDS,
        )
    except RemotionProbeCleanupError:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return None, f"trusted Node version probe failed: {exc}"
    version = probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else ""
    if probe.returncode != 0 or not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+].*)?", version):
        detail = probe.stderr.strip().splitlines()[-1:] or ["invalid version output"]
        return None, f"trusted Node version probe failed: {detail[0]}"
    return version, None


def resolve_remotion_runtime_tools(
    project_dir: Path,
    *, environment: Mapping[str, str] | None = None,
) -> tuple[RemotionRuntimeTools | None, str | None]:
    """Resolve and validate the server-owned Node + locked local CLI pair."""

    node_executable, reason = _configured_node_executable(environment)
    if reason:
        return None, reason
    assert node_executable is not None
    cli_path = (project_dir / REMOTION_CLI_RELATIVE_PATH)
    try:
        cli_resolved = cli_path.resolve(strict=True)
    except OSError as exc:
        return None, f"locked Remotion CLI is missing: {cli_path} ({exc})"
    if not cli_resolved.is_file():
        return None, f"locked Remotion CLI is not a file: {cli_path}"
    try:
        cli_resolved.relative_to(project_dir.resolve())
    except ValueError:
        return None, f"locked Remotion CLI escapes the server-owned project: {cli_path}"
    node_version, reason = _probe_node(node_executable, cwd=project_dir, environment=environment)
    if reason:
        return None, reason
    assert node_version is not None
    from astrid.core.rendering.node_pin import node_pin

    pin = node_pin(project_dir)
    if pin and node_version != pin:
        return None, f"trusted Node is {node_version}, but {project_dir / 'package.json'} pins {pin}"
    return RemotionRuntimeTools(node_executable, node_version, cli_resolved), None


def timeline_requires_remotion(config: Mapping[str, Any]) -> bool:
    """Return whether a timeline contains a non-media Remotion element."""

    clips = config.get("clips")
    if not isinstance(clips, list):
        return False
    return any(
        isinstance(clip, Mapping)
        and clip.get("clipType", "media") != "media"
        for clip in clips
    )


def resolve_remotion_project_dir(environment: Mapping[str, str] | None = None) -> Path:
    """Resolve the server-owned project, with a source-checkout development fallback.

    Only process configuration is consulted; renderer requests cannot select a
    runtime. Invalid explicit configuration must never fall back to the checkout.
    """
    source = os.environ if environment is None else environment
    raw = source.get(REMOTION_PROJECT_DIR_ENV, "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{REMOTION_PROJECT_DIR_ENV} must be an absolute path")
        return candidate.resolve()
    # Development source checkouts may use the conventional location.  A
    # wheel install has no sibling remotion/ tree, so this correctly fails
    # closed until deployment supplies the trusted runtime configuration.
    return (REPO_ROOT / "remotion").resolve()


def remotion_install_problem(project: Path) -> str | None:
    """Explain a missing locked Remotion install and how to provision it."""
    from astrid.core.rendering.node_pin import node_pin

    node_modules = project / "node_modules"
    pin = node_pin(project)
    fix = f"run `npm ci` in {project} with {f'Node {pin}' if pin else 'the pinned Node'} on PATH"
    if not node_modules.is_dir():
        return f"Remotion node_modules is missing: {node_modules}; {fix}"
    missing = [
        f"@banodoco/{name}"
        for name in _REQUIRED_PACKAGES
        if not (node_modules / "@banodoco" / name / "package.json").is_file()
    ]
    if missing:
        return "required Remotion package(s) missing: " + ", ".join(missing) + f"; {fix}"
    if not (project / REMOTION_CLI_RELATIVE_PATH).is_file():
        return f"locked Remotion CLI is missing: {project / REMOTION_CLI_RELATIVE_PATH}; {fix}"
    return None


def remotion_runtime_status(
    *, require_explicit_project: bool = False,
    environment: Mapping[str, str] | None = None,
) -> RemotionRuntimeStatus:
    """Inspect the trusted runtime without mutating it or running npm.

    Development render helpers may use the checkout's conventional ``remotion``
    directory, but admission and worker execution must use an explicitly
    provisioned server-owned project. The strict mode makes that distinction
    explicit without weakening the development parity lane.
    """

    if _UNRESOLVED_PROBE_CUSTODY:
        previous = _UNRESOLVED_PROBE_CUSTODY[0]
        error = RemotionProbeCleanupError(
            "prior Remotion probe child custody is unresolved"
        )
        error.process = previous.process
        error.cleanup_error = previous.cleanup_error
        raise error from previous

    project_dir = None
    try:
        source = os.environ if environment is None else environment
        if require_explicit_project and not source.get(REMOTION_PROJECT_DIR_ENV, "").strip():
            reason = f"{REMOTION_PROJECT_DIR_ENV} must point to the server-owned Remotion project"
            # The source launcher deliberately leaves an uninstalled project unbound.
            # Diagnose that checkout without admitting it as an execution runtime.
            project = (REPO_ROOT / "remotion").resolve()
            if (project / "package.json").is_file():
                install_problem = remotion_install_problem(project)
                if install_problem:
                    reason += f"; {install_problem}"
            return RemotionRuntimeStatus(
                False,
                None,
                reason,
            )

        try:
            project_dir = resolve_remotion_project_dir(source)
        except ValueError as exc:
            return RemotionRuntimeStatus(False, None, str(exc))
        if not project_dir.is_dir():
            return RemotionRuntimeStatus(False, project_dir, f"project directory not found: {project_dir}")
        if not (project_dir / "package.json").is_file():
            return RemotionRuntimeStatus(False, project_dir, f"package.json missing: {project_dir}")
        install_problem = remotion_install_problem(project_dir)
        if install_problem:
            return RemotionRuntimeStatus(False, project_dir, install_problem)
        schema_root_raw = source.get(TIMELINE_SCHEMA_PYTHONPATH_ENV, "").strip()
        if not schema_root_raw:
            return RemotionRuntimeStatus(
                False,
                project_dir,
                f"{TIMELINE_SCHEMA_PYTHONPATH_ENV} must point to the installed timeline schema",
            )
        schema_root = Path(schema_root_raw).expanduser()
        if not schema_root.is_absolute():
            return RemotionRuntimeStatus(
                False,
                project_dir,
                f"{TIMELINE_SCHEMA_PYTHONPATH_ENV} must be an absolute path",
            )
        schema_root = schema_root.resolve()
        schema_package = schema_root / "banodoco_timeline_schema"
        missing_schema_files = [
            name for name in _REQUIRED_SCHEMA_FILES if not (schema_package / name).is_file()
        ]
        if missing_schema_files:
            return RemotionRuntimeStatus(
                False,
                project_dir,
                "installed timeline schema is incomplete under "
                f"{schema_root}: {', '.join(missing_schema_files)}",
            )
        runtime_tools, tools_error = resolve_remotion_runtime_tools(project_dir, environment=source)
        if tools_error:
            return RemotionRuntimeStatus(
                False,
                project_dir,
                tools_error,
            )
        assert runtime_tools is not None
        schema_error = _probe_schema(schema_root, environment=source)
        if schema_error:
            return RemotionRuntimeStatus(False, project_dir, schema_error)
        return RemotionRuntimeStatus(
            True,
            project_dir,
            node_executable=runtime_tools.node_executable,
            node_version=runtime_tools.node_version,
            remotion_cli=runtime_tools.remotion_cli,
        )
    except RemotionProbeCleanupError:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return RemotionRuntimeStatus(False, project_dir, f"trusted Remotion runtime check failed: {exc}")


__all__ = [
    "NODE_EXECUTABLE_ENV",
    "REMOTION_CLI_RELATIVE_PATH",
    "REMOTION_PROJECT_DIR_ENV",
    "RemotionRuntimeTools",
    "RemotionProbeCleanupError",
    "TIMELINE_SCHEMA_PYTHONPATH_ENV",
    "RemotionRuntimeStatus",
    "resolve_remotion_project_dir",
    "resolve_remotion_runtime_tools",
    "remotion_install_problem",
    "remotion_runtime_status",
    "timeline_requires_remotion",
]
