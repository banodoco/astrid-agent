"""Server-owned Remotion runtime discovery for Astrid render admission.

The Python package does not contain a Node checkout.  A release deployment
must point ``ASTRID_REMOTION_PROJECT_DIR`` at its separately provisioned,
server-owned Remotion bundle.  No task/request value is ever consulted here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Iterator, Mapping

from runtime_protocol import platform

from astrid.core.env_vars import (
    ASTRID_NODE_EXECUTABLE,
    ASTRID_PACKS_PATH,
    ASTRID_REMOTION_PROJECT_DIR,
    ASTRID_REMOTION_VALIDATION_LAUNCH,
    ASTRID_REMOTION_VALIDATION_PATH,
    ASTRID_REMOTION_VALIDATION_ROLE,
    ASTRID_REMOTION_VALIDATION_SHA256,
    ASTRID_TIMELINE_SCHEMA_PYTHONPATH,
)
from astrid.core.execution.platform_helpers import owned_by_current_user
from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.rendering import _local_schema, _registry_origin, _remotion_package

REMOTION_PROJECT_DIR_ENV = ASTRID_REMOTION_PROJECT_DIR
NODE_EXECUTABLE_ENV = ASTRID_NODE_EXECUTABLE
TIMELINE_SCHEMA_PYTHONPATH_ENV = ASTRID_TIMELINE_SCHEMA_PYTHONPATH
REMOTION_CLI_RELATIVE_PATH = Path("node_modules/@remotion/cli/remotion-cli.js")
_NODE_PROBE_TIMEOUT_SECONDS = 5
_REGISTRY_PROBE_TIMEOUT_SECONDS = 90
_REQUIRED_PACKAGES = (
    "timeline-composition",
)
_COMPOSITION_PACKAGE_NAME = _remotion_package._COMPOSITION_PACKAGE_NAME
_COMPOSITION_PACKAGE_VERSION = _remotion_package._COMPOSITION_PACKAGE_VERSION
_REQUIRED_COMPOSITION_EXPORTS = _remotion_package._REQUIRED_COMPOSITION_EXPORTS
_composition_package_problem = _remotion_package.composition_package_problem
_SCHEMA_PROBE = """
import importlib.machinery
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[2])
if sys.argv[3] == 'source':
    sys.path.insert(0, str(root))
elif sys.argv[3] != 'installed' or not sys.flags.isolated:
    raise RuntimeError('schema validation requires an isolated installed interpreter')
# Check the selected reader before executing any of its package initializers.
# Installed mode uses the interpreter's distribution; source mode uses only
# the launch-selected checkout, never the configured schema as an import root.
specs = {}
for name in ('astrid', 'astrid.core', 'astrid.core.rendering',
             'astrid.core.rendering._local_schema', 'astrid.core.timeline',
             'astrid.core.timeline.banodoco_schema'):
    parent = name.rpartition('.')[0]
    search = specs[parent].submodule_search_locations if parent else sys.path
    spec = importlib.machinery.PathFinder.find_spec(name.rsplit('.', 1)[-1], search)
    path = root.joinpath(*name.split('.'))
    expected = path / '__init__.py' if path.is_dir() else path.with_suffix('.py')
    if (spec is None or not expected.is_file()
            or any(p.is_symlink() for p in (expected, *expected.parents))
            or spec.origin != str(expected)
            or not isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            or (spec.submodule_search_locations is not None
                and list(spec.submodule_search_locations) != [str(path)])):
        raise RuntimeError('canonical schema reader origin differs from selected Agent source')
    specs[name] = spec
from astrid.core.rendering import _local_schema
if pathlib.Path(_local_schema.__file__) != root / 'astrid/core/rendering/_local_schema.py':
    raise RuntimeError('canonical schema helper has a foreign origin')
contract = _local_schema.load_verified_schema(root, sys.argv[1])
origin = pathlib.Path(sys.argv[1])
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



class RemotionValidationError(RuntimeError):
    """Validation failed; uncertain original child handles remain attached."""

    probe_receipt: Mapping[str, Any]


class RemotionProbeCleanupError(RemotionValidationError):
    """A failed probe whose child process could not be proved settled."""

    process: subprocess.Popen[str]
    cleanup_error: BaseException
    original_error: BaseException


_UNRESOLVED_PROBE_CUSTODY: list[RemotionProbeCleanupError] = []


@contextmanager
def remotion_render_lock() -> Iterator[None]:
    """Expose the renderer lock through the server-owned runtime boundary."""

    from astrid.packs.rendering.rendering.renderers.remotion import lock

    if lock.remotion_render_lock_held():
        yield
        return
    with lock.remotion_render_lock():
        yield


def _validation_timing(phase: str):
    """Bounded diagnostic metadata; no launch paths, arguments or environment."""
    def decorate(operation):
        @wraps(operation)
        def measured(*args, **kwargs):
            started = time.monotonic()
            outcome = "failed"
            try:
                result = operation(*args, **kwargs)
                outcome = "completed"
                return result
            finally:
                try:
                    print(json.dumps({"diagnostic": "astrid.remotion_validation_phase",
                                      "phase": phase, "outcome": outcome,
                                      "process_id": os.getpid(),
                                      "elapsed_ms": round((time.monotonic() - started) * 1000)}),
                          file=sys.stderr, flush=True)
                except Exception:
                    pass  # Optional diagnostics never replace validation failures.
        return measured
    return decorate


def _probe_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    from astrid.core.subprocess_env import _SAFE_BASE_ENV, _WINDOWS_BASE_ENV

    keys = _SAFE_BASE_ENV | (_WINDOWS_BASE_ENV if sys.platform == "win32" else frozenset())
    source = os.environ if environment is None else environment
    return {"PATH": os.defpath, **{key: value for key, value in source.items() if key in keys}}


@_validation_timing("owned_probe")
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
                fatal.original_error = primary
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


_VALIDATION_MAX_BYTES = 16 * 1024 * 1024
_UNSUPPORTED_PYTHON_STARTUP = frozenset({'PYTHONUSERBASE', 'PYTHONNOUSERSITE', 'PYTHONSAFEPATH',
    'PYTHONPYCACHEPREFIX', 'PYTHONPLATLIBDIR', 'PYTHONCASEOK', 'PYTHONUTF8',
    'PYTHONCOERCECLOCALE', 'PYTHONSTARTUP'})

_VALIDATION_SCHEMA = "astrid.remotion-launch-validation/v1"
_NODE_ENV_KEYS = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR")
if sys.platform == "win32":
    from astrid.core.subprocess_env import _WINDOWS_BASE_ENV

    _NODE_ENV_KEYS += tuple(sorted(_WINDOWS_BASE_ENV))
_SCHEMA_ENV_KEYS = ("HOME", "LANG", "LC_ALL", "LC_CTYPE")
if sys.platform == "win32":
    _SCHEMA_ENV_KEYS += tuple(sorted(_WINDOWS_BASE_ENV))
_REF_ENV_KEYS = (ASTRID_REMOTION_VALIDATION_PATH, ASTRID_REMOTION_VALIDATION_SHA256,
                 ASTRID_REMOTION_VALIDATION_LAUNCH, ASTRID_REMOTION_VALIDATION_ROLE)


def probe_context(*, environment: Mapping[str, str], python_executable: str) -> dict[str, Any]:
    """Project the documented effective probes; never copy task secrets."""
    home = environment.get("HOME")
    if sys.platform == "win32" and not home:
        home = environment.get("USERPROFILE")
    if not isinstance(home, str) or not home or not Path(home).is_absolute():
        raise RemotionValidationError("runtime validation needs an explicit absolute HOME")
    # Only these projections reach the owned Node/schema probes. Ambient
    # loader and interpreter controls are neither child inputs nor key inputs.
    python = Path(python_executable)
    if not python.is_absolute():
        raise RemotionValidationError("runtime validation needs an absolute lexical interpreter")
    if any(not isinstance(environment[k], str) for k in _NODE_ENV_KEYS if k in environment):
        raise RemotionValidationError("runtime probe environment values must be strings")
    return {"policy": "clean-schema-env/v1", "python_executable": str(python),
            "schema_profile": 'installed' if _installed_registry_profile(environment) else 'source',
            "node_env": {k: (_dedupe_path(environment[k]) if k == "PATH" else environment[k])
                         for k in _NODE_ENV_KEYS if k in environment},
            "schema_env": {k: environment[k] for k in _SCHEMA_ENV_KEYS if k in environment}}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _file_binding(path: Path, *, digest_memo: dict | None = None) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise RemotionValidationError("runtime validation input is not a file")
    before = resolved.stat()
    def identity(facts):
        common = (facts.st_dev, facts.st_ino, facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns)
        # Windows stat/fstat synthesize different permission bits for paths
        # and CRT descriptors. File identity and content timestamps still bind.
        return common if sys.platform == "win32" else (*common, facts.st_mode, facts.st_uid)
    key = identity(before)
    cached = digest_memo.get(str(resolved)) if digest_memo is not None else None
    # Only reuse old, unchanged files within this held-lock launch. Recent
    # timestamps are conservatively re-read to avoid racily clean entries.
    reusable = (cached is not None and cached[0] == key and time.time_ns() >= cached[2]
                and cached[2] - max(before.st_mtime_ns, before.st_ctime_ns) >= 2_000_000_000)
    if reusable:
        content_digest = cached[1]
    else:
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            if digest_memo is not None and identity(os.fstat(stream.fileno())) != key:
                raise RemotionValidationError("runtime inputs changed during content binding")
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            if digest_memo is not None and identity(os.fstat(stream.fileno())) != key:
                raise RemotionValidationError("runtime inputs changed during content binding")
        content_digest = digest.hexdigest()
    facts = resolved.stat()
    if digest_memo is not None:
        if identity(facts) != key or path.resolve(strict=True) != resolved:
            raise RemotionValidationError("runtime inputs changed during content binding")
        if not reusable:
            digest_memo[str(resolved)] = (key, content_digest, time.time_ns())
    return {"path": str(path.absolute()), "resolved": str(resolved),
            "sha256": content_digest, "mode": stat.S_IMODE(facts.st_mode),
            "uid": facts.st_uid, "device": facts.st_dev, "inode": facts.st_ino}


# Names the OS or Python creates on its own (Finder, bytecode); never installation inputs.
_GENERATED_NAMES = frozenset({"__pycache__", ".DS_Store"})


def _startup_names(directory: Path) -> list[str]:
    """Entries of a sys.path directory that Python could import (packages, modules, extension modules, .pth).

    Only these can change which modules are available; Finder, editor and log files cannot.
    """
    import importlib.machinery
    suffixes = set(importlib.machinery.all_suffixes()) | {".pth"}
    return sorted(p.name for p in directory.iterdir()
                  if p.name != "__pycache__" and (p.is_dir() or p.suffix in suffixes))


def _describe_corpus_change(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    """Plain-language account of what differs between two _registry_inputs results."""
    parts: list[str] = []
    for key in sorted(set(old['inventory']) | set(new['inventory'])):
        before, after = old['inventory'].get(key), new['inventory'].get(key)
        if before == after:
            continue
        where = key[len('startup:'):] if key.startswith('startup:') else key
        if isinstance(before, list) and isinstance(after, list):
            for name in sorted(set(after) - set(before))[:3]:
                parts.append(f"a new module or file `{name}` appeared in {where}")
            for name in sorted(set(before) - set(after))[:3]:
                parts.append(f"`{name}` was removed from {where}")
        else:
            parts.append(f"{where} changed")
    if not parts and old['files_sha256'] != new['files_sha256']:
        parts.append("the contents of a registry source file changed")
    return "; ".join(parts[:4]) or "the registry corpus changed"


def _tree_files(root: Path) -> list[Path]:
    """Immutable installation inputs; generated Python bytecode is separate."""
    found: dict[str, Path] = {}
    visited: set[str] = set()
    def visit(directory: Path) -> None:
        resolved = str(directory.resolve(strict=True))
        if resolved in visited:
            return
        visited.add(resolved)
        for path in sorted(directory.iterdir()):
            if path.name in _GENERATED_NAMES | {".cache", ".git"} or path.suffix == ".pyc":
                continue
            if path.is_dir():
                visit(path)
            elif path.is_file():
                found[str(path.absolute())] = path
            else:
                raise RemotionValidationError("runtime installation has an unreadable input")
    visit(root)
    return list(found.values())


@_validation_timing("complete_inputs")
def _runtime_inputs(project_dir: Path, environment: Mapping[str, str], python: str,
                    closure: Mapping[str, Any], *, digest_memo: dict | None = None) -> dict[str, Any]:
    project = project_dir.resolve(strict=True)
    configured = environment.get(NODE_EXECUTABLE_ENV, "")
    schema_raw = environment.get(TIMELINE_SCHEMA_PYTHONPATH_ENV, "")
    if not configured or not Path(configured).is_absolute() or not schema_raw or not Path(schema_raw).is_absolute():
        raise RemotionValidationError("runtime validation requires explicit Node and schema paths")
    node = Path(configured)
    if not os.access(node, os.X_OK):
        raise RemotionValidationError("configured Node is not executable")
    try:
        schema = _local_schema.verified_schema_path(REPO_ROOT, schema_raw)
    except (OSError, ValueError) as exc:
        raise RemotionValidationError(f"runtime schema inputs changed or invalid identity: {exc}") from exc
    cli = project / REMOTION_CLI_RELATIVE_PATH
    cli.resolve(strict=True).relative_to(project)
    composition_problem = _composition_package_problem(project)
    if composition_problem:
        raise RemotionValidationError(composition_problem)
    roots = [project / "node_modules", schema.parent]
    if (project / "src").is_dir():
        roots.append(project / "src")
    roots.extend(Path(p) for p in closure.get("package_roots", []))
    paths = [node, Path(python), cli, project / "package.json", project / "package-lock.json", Path(__file__),
             Path(_remotion_package.__file__), Path(_local_schema.__file__),
             REPO_ROOT / "runtime.lock.json", schema,
             *(REPO_ROOT / p for p in _local_schema.READER_INPUTS)]
    paths.extend(p for p in project.iterdir() if p.is_file() and p.suffix in {".ts", ".js", ".mjs", ".cjs", ".json"})
    cfg = Path(python).parent.parent / "pyvenv.cfg"
    if cfg.is_file():
        paths.append(cfg)
    paths.extend(Path(p) for p in closure.get("module_files", []))
    for root in roots:
        paths.extend(_tree_files(root))
    startup: dict[str, Any] = {}
    for raw in closure.get("search_paths", []):
        directory = Path(raw)
        if not directory.is_dir():
            startup[raw] = 'file' if directory.is_file() else 'absent'
            if directory.is_file():
                paths.append(directory)
            continue
        # Detect changed startup/optional module availability as well as bytes.
        startup[raw] = _startup_names(directory)
        paths.extend(directory.glob("*.pth"))
        for name in ("sitecustomize.py", "sitecustomize.pyc", "usercustomize.py", "usercustomize.pyc"):
            p = directory / name
            if p.is_file():
                paths.append(p)
    unique = {str(p.absolute()): p for p in paths}
    bindings = [_file_binding(unique[k], digest_memo=digest_memo) for k in sorted(unique)]
    return {"project": str(project), "configured_project": environment.get(REMOTION_PROJECT_DIR_ENV),
            "configured_node": configured, "configured_schema": schema_raw,
            "schema": str(schema), "cli": str(cli.resolve(strict=True)),
            "lexical_python": python, "startup_inventory": startup,
            "file_count": len(bindings), "files_sha256": hashlib.sha256(_canonical(bindings)).hexdigest()}


def _schema_probe_source() -> str:
    # The installed import closure, optional presence and startup facts are output
    # by the same successful clean probe, not guessed from the launch owner's env.
    return _SCHEMA_PROBE.replace("print(origin)", """
import json, os, importlib.util
files = sorted({str(pathlib.Path(p).resolve()) for m in tuple(sys.modules.values())
                for p in (getattr(m, '__file__', None), getattr(m, '__cached__', None))
                if p and pathlib.Path(p).is_file()})
roots = sorted({str(pathlib.Path(p).resolve()) for m in tuple(sys.modules.values())
                for p in getattr(m, '__path__', ()) if pathlib.Path(p).is_dir()})
print(json.dumps({'origin': str(origin), 'module_files': files, 'package_roots': roots,
                 'search_paths': [str(pathlib.Path(p or '.').absolute()) for p in sys.path],
                 'search_path_spellings': sys.path, 'probe_cwd': os.getcwd(), 'optional_idna': importlib.util.find_spec('idna') is not None,
                 'startup': {'executable': sys.executable, 'prefix': sys.prefix,
                             'exec_prefix': sys.exec_prefix, 'base_prefix': sys.base_prefix,
                             'base_exec_prefix': sys.base_exec_prefix,
                             'version': sys.version, 'cache_tag': sys.implementation.cache_tag,
                             'platform': sys.platform, 'abi_flags': getattr(sys, 'abiflags', ''),
                             'locale_env': {k: os.environ[k] for k in __ASTRID_SCHEMA_ENV_KEYS__ if k in os.environ},
                             'filesystem_encoding': sys.getfilesystemencoding(),
                             'stdout_encoding': sys.stdout.encoding}}))
""").replace("__ASTRID_SCHEMA_ENV_KEYS__", repr(_SCHEMA_ENV_KEYS))


_REGISTRY_KINDS = ('effects', 'animations', 'transitions')
_REGISTRY_SELECTORS = ('ASTRID_PACKS_PATH', 'ASTRID_SOURCE_STATE', 'XDG_STATE_HOME')
# Git variables that only shape interactive use (editors, pagers, prompts, transport, commit identity):
# they change nothing Git reads. Agent and editor shells set GIT_EDITOR, so refusing them refused every launch.
_INTERACTIVE_GIT_ENV = frozenset({
    'GIT_EDITOR', 'GIT_SEQUENCE_EDITOR', 'GIT_PAGER', 'GIT_TERMINAL_PROMPT', 'GIT_ASKPASS',
    'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_SSH_VARIANT', 'GIT_MERGE_AUTOEDIT',
    'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL',
})


def _validate_remotion_worker_environment(environment: Mapping[str, str]) -> None:
    """Check controls delivered to a worker, before its interpreter starts."""
    forbidden = sorted(k for k in environment
                       if k.startswith(('DYLD_', 'LD_'))
                       or (k.startswith('GIT_') and k not in _INTERACTIVE_GIT_ENV)
                       or k in _UNSUPPORTED_PYTHON_STARTUP
                       or k in {'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__', 'PYTHONHOME',
                                'NODE_OPTIONS', 'NODE_PATH', 'NODE_ICU_DATA', 'OPENSSL_CONF'})
    if forbidden:
        raise RemotionValidationError(
            f"Remotion worker environment refuses {', '.join(forbidden)}; "
            "remove these variables from command.env, required_env and env_passthrough")


def _registry_context(environment: Mapping[str, str], *, generator: bool) -> dict[str, str]:
    from astrid.core.subprocess_env import _SAFE_BASE_ENV, _WINDOWS_BASE_ENV
    if _installed_registry_profile(environment) and any(environment.get(name) for name in ('PYTHONPATH', ASTRID_PACKS_PATH)):
        raise RemotionValidationError('installed registry discovery refuses explicit import or pack roots')
    keys = set(_SAFE_BASE_ENV) | {'ASTRID_HOST_EXECUTION_PROFILE', 'ASTRID_PACKS_PATH',
                                  TIMELINE_SCHEMA_PYTHONPATH_ENV, NODE_EXECUTABLE_ENV}
    if sys.platform == 'win32':
        keys.update(_WINDOWS_BASE_ENV)
    if not generator:
        keys.update((*_REGISTRY_SELECTORS, 'PYTHONPATH', 'XDG_CONFIG_HOME'))
    context = {k: environment[k] for k in sorted(keys) if k in environment}
    # This mapping is the registry child's complete environment. Ambient
    # Git/startup controls omitted here cannot influence that child.
    probe_context(environment=context, python_executable=sys.executable)
    for name, value in context.items():
        if not isinstance(value, str):
            raise RemotionValidationError(f'{name} must be a string')
    for name in ('ASTRID_SOURCE_STATE', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME'):
        if context.get(name) and not Path(context[name]).is_absolute():
            raise RemotionValidationError(f'{name} must be an absolute launch-known path')
    if context.get('PYTHONPATH') and any(not raw or not Path(raw).is_absolute() for raw in context['PYTHONPATH'].split(os.pathsep)):
        raise RemotionValidationError('PYTHONPATH must contain absolute paths; remove empty or relative entries')
    return _normalize_registry_selectors(context)


def _resolved_path_text(raw: str) -> str:
    return str(Path(raw).expanduser().resolve())


def _dedupe_path(value: str) -> str:
    """Drop repeated PATH entries: lookup takes the first match, so repeats change nothing.

    A shell that sourced its environment twice must not look like a different
    (unvalidated) context and make a healthy host unusable.
    """
    return os.pathsep.join(dict.fromkeys(value.split(os.pathsep)))


def _normalize_registry_selectors(context: dict[str, str]) -> dict[str, str]:
    """Drop selector entries that restate what discovery already includes.

    PYTHONPATH naming only this checkout and ASTRID_PACKS_PATH naming only the
    built-in packs or an active managed source pack root select the same
    registry corpus as their absence; a genuinely different path keeps its own
    context. Launch settling and render-time matching both use this function.
    """
    if context.get('PATH'):
        context['PATH'] = _dedupe_path(context['PATH'])
    checkout = _resolved_path_text(str(REPO_ROOT))
    if context.get('PYTHONPATH'):
        kept = [raw for raw in context['PYTHONPATH'].split(os.pathsep) if _resolved_path_text(raw) != checkout]
        if kept:
            context['PYTHONPATH'] = os.pathsep.join(kept)
        else:
            del context['PYTHONPATH']
    if context.get(ASTRID_PACKS_PATH):
        implied = {_resolved_path_text(str(REPO_ROOT / 'astrid' / 'packs'))}
        try:
            from astrid.core.pack.source_setup import active_source_inventory
            implied.update(_resolved_path_text(str(source.pack_root))
                           for source in active_source_inventory(verify=False).sources)
        except Exception:
            pass  # Unknown managed roots stay distinct contexts.
        # Relative entries resolve against the probe's own root; they stay distinct.
        kept = [raw for raw in context[ASTRID_PACKS_PATH].split(os.pathsep) if raw and not (
            Path(raw).expanduser().is_absolute() and _resolved_path_text(raw) in implied)]
        if kept:
            context[ASTRID_PACKS_PATH] = os.pathsep.join(kept)
        else:
            del context[ASTRID_PACKS_PATH]
    return context


def _registry_config_files(environment: Mapping[str, str]) -> list[Path]:
    import shutil
    home_raw = environment.get('HOME')
    if sys.platform == 'win32' and not home_raw:
        home_raw = environment.get('USERPROFILE')
    if not home_raw:
        raise RemotionValidationError('registry validation needs an explicit home directory')
    home = Path(home_raw)
    paths = [home / '.gitconfig', Path(environment.get('XDG_CONFIG_HOME', str(home / '.config'))) / 'git/config',
             Path('/etc/gitconfig')]
    git = shutil.which('git', path=environment.get('PATH', os.defpath))
    if git:
        paths.append(Path(git).resolve().parent.parent / 'etc/gitconfig')
    return list(dict.fromkeys(paths))


def _refuse_git_config_indirection(paths: list[Path]) -> None:
    import configparser
    for path in paths:
        if not path.exists():
            continue
        parser = configparser.RawConfigParser(strict=False, allow_no_value=True)
        try:
            parser.read_string(path.read_text())
        except (OSError, UnicodeError, configparser.Error) as exc:
            raise RemotionValidationError('selected Git configuration cannot be bound') from exc
        if any(section.lower().split(None, 1)[0] in {'include', 'includeif'} for section in parser.sections()):
            raise RemotionValidationError('registry validation refuses unqualified Git include indirection')


def _managed_registry_configuration() -> tuple[list[str], list[Path]]:
    """Locate config from unverified records; acquire no Git result authority."""
    from astrid.core.pack.source_setup import active_source_inventory
    roots: list[str] = []
    files: list[Path] = []
    for source in active_source_inventory(verify=False).sources:
        checkout = Path(source.pack_root)
        if source.pack_subpath != '.':
            for _ in source.pack_subpath.split('/'):
                checkout = checkout.parent
        git_dir = checkout / '.git'
        if git_dir.is_symlink() or git_dir.is_file():
            raise RemotionValidationError('managed Git directory indirection is unsupported')
        if (git_dir / 'commondir').exists():
            raise RemotionValidationError('managed Git common-directory indirection is unsupported')
        configs = [git_dir / 'config', git_dir / 'config.worktree']
        _refuse_git_config_indirection(configs)
        roots.append(str(git_dir))
        files.extend(configs)
    return roots, files


def _registry_observation() -> dict[str, Any]:
    """Fresh discovery under this process's actual renderer/generator context."""
    import importlib
    import shutil
    import astrid
    from astrid.core.element.registry import clear_default_registry_cache
    from astrid.core.pack.source_setup import active_source_inventory, source_state_path
    from astrid.core.pack.discovery import discover_pack_metadata
    from astrid.packs.rendering.rendering.renderers.remotion import lock, run
    # run is imported from the same selected product tree as this probe.
    # Its existing carrier is the only installed generator authority; never
    # recover a generator from the launch checkout.
    generator = run.gen_effect_registry
    if generator is None:
        raise RemotionValidationError(
            'installed registry generator carrier is unavailable: '
            'the selected Astrid package does not carry gen_effect_registry'
        )
    config_files = _registry_config_files(os.environ)
    _refuse_git_config_indirection(config_files)
    managed_git_roots, managed_config_files = _managed_registry_configuration()
    clear_default_registry_cache()
    state = generator.compute_generated_registry_state()  # include_local=True
    inventory = active_source_inventory(verify=True)
    packs = discover_pack_metadata(project_root=generator.TOOLS_DIR)
    roots = [str(Path(astrid.__file__).resolve().parent / 'packs'),
             str(Path(generator.TOOLS_DIR) / 'astrid/packs/local')]
    roots.extend(str(source.pack_root) for source in inventory.sources)
    for raw in os.environ.get(ASTRID_PACKS_PATH, '').split(os.pathsep):
        if raw:
            root = Path(raw).expanduser()
            roots.append(str(root if root.is_absolute() else Path(generator.TOOLS_DIR) / root))
    roots.extend(str(pack.pack_dir) for pack in packs)
    # Only ordinary Git directories qualified before any verified discovery.
    roots.extend(managed_git_roots)
    files = sorted({str(Path(value).resolve()) for module in tuple(sys.modules.values())
                    for value in (getattr(module, '__file__', None), getattr(module, '__cached__', None))
                    if value and Path(value).is_file()})
    package_roots = sorted({str(Path(value).resolve()) for module in tuple(sys.modules.values())
                            for value in getattr(module, '__path__', ()) if Path(value).is_dir()})
    optional_files = [str(source_state_path()), *(str(path) for path in (*config_files, *managed_config_files))]
    git = shutil.which('git')
    if inventory.sources and not git:
        raise RemotionValidationError('managed registry corpus requires the selected Git executable')
    if git:
        files.append(str(Path(git).resolve()))
    from astrid.core.source_identity import installed_runtime_origins
    dependency_origins = (
        {'runtime_dependency_origins': installed_runtime_origins(_registry_origin.selected_root(__file__))}
        if _installed_registry_profile(os.environ) else {}
    )
    return {'state': state, 'roots': roots, 'optional_files': optional_files,
            'module_files': files, 'package_roots': package_roots,
            'search_paths': [str(Path(value or '.').absolute()) for value in sys.path],
            'search_path_spellings': sys.path, 'probe_cwd': os.getcwd(),
            'generator_origin': generator.__file__,
            'astrid_origin': astrid.__file__,
            'runtime_origin': __file__,
            **dependency_origins,
            'managed_inventory_identity': inventory.identity,
            'lock_path': str(lock._resolved_lock_path()),
            'startup': {'executable': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix}}


def _registry_inputs(observation: Mapping[str, Any], *, digest_memo: dict | None = None,
                     changed_after_ns: int | None = None) -> dict[str, Any]:
    paths = [Path(raw) for raw in observation['module_files']]
    inventory: dict[str, Any] = {}
    for raw in (*observation['roots'], *observation['package_roots']):
        root = Path(raw)
        if not root.exists():
            inventory[raw] = None
        elif root.is_dir():
            # Membership is bound, including directories with no current winner.
            files = _tree_files(root)
            inventory[raw] = sorted(str(p.relative_to(root)) for p in files)
            paths.extend(files)
        else:
            paths.append(root)
            inventory[raw] = 'file'
    for raw in observation['optional_files']:
        path = Path(raw)
        inventory[raw] = path.exists()
        if path.exists():
            paths.append(path)
    for raw in observation['search_paths']:
        root = Path(raw)
        if not root.is_dir():
            inventory['startup:' + raw] = 'file' if root.is_file() else 'absent'
            if root.is_file():
                paths.append(root)
            continue
        if root.is_dir():
            inventory['startup:' + raw] = _startup_names(root)
            paths.extend(root.glob('*.pth'))
            for name in ('sitecustomize.py', 'sitecustomize.pyc', 'usercustomize.py', 'usercustomize.pyc'):
                if (root / name).is_file():
                    paths.append(root / name)
    unique = {str(path.absolute()): path for path in paths}
    if changed_after_ns is not None:
        # Single-probe fence: nothing the probe read may have changed since it started.
        for path in unique.values():
            if path.suffix != '.pyc' and max(path.stat().st_mtime_ns, path.stat().st_ctime_ns) > changed_after_ns:
                raise RemotionValidationError('registry corpus changed during owned discovery')
    bindings = [_file_binding(unique[key], digest_memo=digest_memo) for key in sorted(unique)]
    return {'inventory': inventory, 'files_sha256': hashlib.sha256(_canonical(bindings)).hexdigest(),
            'file_count': len(bindings)}


def _require_completed_probe(receipt: Mapping[str, Any], *, phase: str) -> None:
    if (receipt.get('original_wait_exit_code') != 0
            or receipt.get('retained_set_final_census_complete') is not True
            or receipt.get('custody_admitted') is not True):
        # Only fixed labels, booleans, bounded integers and fixed type names
        # cross this diagnostic boundary; the original receipt has argv/cwd.
        fields = ('original_wait_exit_code', 'retained_set_final_census_complete', 'custody_admitted')
        checks = (receipt.get(fields[0]) == 0, receipt.get(fields[1]) is True,
                  receipt.get(fields[2]) is True)
        code, pid = receipt.get(fields[0]), receipt.get('original_child_pid')
        types = {type(None): 'none', bool: 'bool', int: 'int', float: 'float',
                 str: 'str', list: 'list', dict: 'dict'}
        safe = {
            'phase': phase if type(phase) is str and phase in (
                'registry_discovery', 'registry_generator', 'node_version', 'schema', 'qualifying',
            ) else 'unknown',
            'failed_checks': [name for name, passed in zip(fields, checks) if not passed],
            'original_wait_exit_code': code if type(code) is int and -(2**31) <= code < 2**31 else None,
            'exit_code_type': types.get(type(code), 'other'),
            'exit_code_missing': fields[0] not in receipt,
            'original_child_pid': pid if type(pid) is int and 0 < pid < 2**31 else None,
            'original_child_pid_missing': 'original_child_pid' not in receipt,
            'custody_admitted': checks[2], 'custody_admitted_missing': fields[2] not in receipt,
            'custody_admitted_type': types.get(type(receipt.get(fields[2])), 'other'),
            'census_complete': checks[1], 'census_complete_missing': fields[1] not in receipt,
            'census_complete_type': types.get(type(receipt.get(fields[1])), 'other'),
        }
        error = RemotionValidationError(
            'owned validation lacks positive original wait and completed custody; probe_check='
            + json.dumps(safe, sort_keys=True, separators=(',', ':')))
        error.probe_check = safe
        raise error


def _keep_probe_stderr(error: RemotionValidationError, result: Any, *, phase: str) -> None:
    """Keep a failed probe's stderr in an owner-only log; the message names only that path.

    The diagnostic boundary carries fixed labels, so the stderr itself stays out
    of the error. Without it, a probe exiting 1 under load was unexplainable.
    """
    stderr = getattr(result, 'stderr', None)
    if not stderr:
        return
    try:
        directory = Path(tempfile.gettempdir()) / 'astrid-probe-logs'
        directory.mkdir(mode=0o700, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f'{phase}-', suffix='.stderr.log', dir=directory)
        with os.fdopen(fd, 'w', encoding='utf-8', errors='replace') as handle:
            handle.write(stderr if isinstance(stderr, str) else stderr.decode('utf-8', 'replace'))
    except OSError:
        return
    error.args = (f'{error.args[0]}; probe stderr kept in {name}',) + tuple(error.args[1:])
    error.probe_stderr_path = name


def _owned_registry_probe(argv: list[str], *, cwd: Path, env: Mapping[str, str],
                          phase: str) -> tuple[subprocess.CompletedProcess, dict[str, Any]]:
    """Bound registry work separately; timeout never becomes readiness or cleanup proof."""
    try:
        return _owned_probe(argv, cwd=cwd, env=env, timeout=_REGISTRY_PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as primary:
        error = RemotionValidationError(
            f"registry validation not proven: {phase} exceeded {_REGISTRY_PROBE_TIMEOUT_SECONDS} seconds")
        receipt = dict(getattr(primary, "probe_receipt", {}))
        receipt.update(outcome="REGISTRY_TIMEOUT_NOT_PROVEN", phase=phase,
                       timeout_seconds=_REGISTRY_PROBE_TIMEOUT_SECONDS)
        error.probe_receipt = receipt
        for name in ("process", "cleanup_error"):
            if hasattr(primary, name):
                setattr(error, name, getattr(primary, name))
        raise error from primary


def _installed_registry_profile(environment: Mapping[str, str]) -> bool:
    """Use the existing installed/source discriminator for registry children."""
    return (environment.get('ASTRID_HOST_EXECUTION_PROFILE') == 'installed'
            or os.environ.get('ASTRID_HOST_EXECUTION_PROFILE') == 'installed')


def _registry_probe(environment: Mapping[str, str], python: str, *, generator: bool = False,
                    installed_source: tuple[str, str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    from astrid.packs.rendering.rendering.renderers.remotion import run
    installed = installed_source is not None or _installed_registry_profile(environment)
    if installed and environment.get('PYTHONPATH'):
        raise RemotionValidationError('installed registry discovery refuses PYTHONPATH')
    if installed and environment.get(ASTRID_PACKS_PATH):
        raise RemotionValidationError('installed registry discovery refuses explicit pack roots')
    if installed:
        cwd = Path(tempfile.gettempdir()).resolve()
        try:
            argv = _registry_origin.installed_argv(python, __file__, installed_source=installed_source)
        except FileNotFoundError as exc:
            raise RemotionValidationError('installed registry generator carrier is unavailable') from exc
        except (OSError, ValueError) as exc:
            raise RemotionValidationError('installed registry launch source binding is unavailable') from exc
        environment = {**environment, 'ASTRID_HOST_EXECUTION_PROFILE': 'installed'}
    else:
        script_directory = REPO_ROOT / 'scripts' if generator else Path(run.__file__).resolve().parents[2]
        # Bind imports to the caller's product tree even when it is already
        # later on sys.path (for example, an installed wheel behind a checkout
        # supplied in PYTHONPATH). Keep the remaining discovery paths intact.
        source = ('import sys,json; sys.path[0]=sys.argv[2]; '
                  'sys.path.insert(0,sys.argv[1]) if sys.argv[1] not in sys.path else None; '
                  'from astrid.core.rendering.remotion_runtime import _registry_observation; '
                  'print(json.dumps(_registry_observation(),sort_keys=True))')
        cwd = REPO_ROOT if generator else script_directory
        argv = [python, '-X', 'utf8', '-c', source, str(REPO_ROOT), str(script_directory)]
    result, receipt = _owned_registry_probe(argv, cwd=cwd, env=environment, phase='registry_discovery')
    try:
        _require_completed_probe(receipt, phase='registry_discovery')
    except RemotionValidationError as error:
        _keep_probe_stderr(error, result, phase='registry_discovery')
        raise
    try:
        observation = json.loads(result.stdout)
        if result.returncode or observation['startup']['executable'] != python:
            raise ValueError('registry source/interpreter differs')
        if installed:
            _registry_origin.validate_observation(observation, __file__, cwd, installed_source)
        elif observation['runtime_origin'] != str(Path(__file__).resolve()):
            raise ValueError('registry source/interpreter differs')
        hashes = observation['state']['content_hashes']
        if (set(hashes) != set(_REGISTRY_KINDS) or not observation['state'].get('hash')
                or any(not re.fullmatch('[0-9a-f]{64}', value) for value in hashes.values())):
            raise ValueError('registry state is incomplete')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        error = RemotionValidationError('owned registry discovery failed')
        error.probe_receipt = receipt
        raise error from exc
    return observation, receipt


@_validation_timing("registry_settle")
def _settle_registries(project: Path, contexts: Mapping[str, Any], python: str, *, expected_state: Any = None,
                       installed_source: tuple[str, str] | None = None) -> dict[bytes, Any]:
    from astrid.packs.rendering.rendering.renderers.remotion import lock, run
    if not lock.remotion_render_lock_held():
        raise RemotionValidationError('registry settling requires the original held Remotion lock')
    output_paths = run._registry_output_paths(project)
    selected: dict[bytes, Any] = {}
    installed = installed_source is not None or _installed_registry_profile(os.environ) or any(
        _installed_registry_profile(env) for paths in contexts.values()
        for env in ([paths] if isinstance(paths, Mapping) else paths))
    if run.gen_effect_registry is None:
        if installed:
            raise RemotionValidationError('installed registry generator carrier is unavailable')
        # Preserve source-only provisioned output bindings without a generator.
        for paths in contexts.values():
            for environment in ([paths] if isinstance(paths, Mapping) else paths):
                context = _registry_context(environment, generator=False)
                context[REMOTION_PROJECT_DIR_ENV] = str(project)
                key = _canonical(context)
                selected[key] = {'mode': 'provisioned_only', 'outputs': [_file_binding(p) for p in output_paths],
                                 'state_file': _file_binding(project / run._REGISTRY_STATE_PATH)
                                 if (project / run._REGISTRY_STATE_PATH).exists() else None}
        return selected
    probes: dict[bytes, Any] = {}
    expected = expected_state
    generator_env = None
    generator_origin = None
    digest_memo: dict = {}  # One launch only; never persisted or shared.
    for paths in contexts.values():
        for environment in ([paths] if isinstance(paths, Mapping) else paths):
            renderer = _registry_context(environment, generator=False)
            renderer[REMOTION_PROJECT_DIR_ENV] = str(project)
            # The generator is only run (below) to repair outputs, which are then
            # hash-checked against the expected state; it is not probed separately.
            generator_env = _registry_context(environment, generator=True)
            generator_env[REMOTION_PROJECT_DIR_ENV] = str(project)
            key = _canonical({'environment': renderer, 'generator_startup': False})
            if key not in probes:
                started_ns = time.time_ns()
                probe_environment = {**renderer, 'ASTRID_HOST_EXECUTION_PROFILE': 'installed'} if installed else renderer
                observation, discovery_wait = _registry_probe(probe_environment, python, generator=False,
                    **({'installed_source': installed_source} if installed_source is not None else {}))
                _require_completed_probe(discovery_wait, phase='registry_discovery')
                bound = _registry_inputs(observation, digest_memo=digest_memo, changed_after_ns=started_ns)
                if observation['lock_path'] != str(lock._resolved_lock_path()):
                    raise RemotionValidationError('registry generator selected another Remotion lock')
                generator_origin = observation.get('generator_origin')
                probes[key] = {'observation': observation, 'inputs': bound,
                               'original_waits': {'discovery': discovery_wait}}
            state = probes[key]['observation']['state']
            if expected is None:
                expected = state
            if state != expected:
                raise RemotionValidationError('renderer/generator sharing contexts expect different registry contents')
            selected[_canonical(renderer)] = {'mode': 'source_settled', 'renderer': probes[key]}
    cached = run._read_registry_state(project)  # Fresh parse; never trust an LRU.
    current_hashes = run._registry_output_content_hashes(project)
    generator_wait = None
    if cached != expected or current_hashes != expected['content_hashes']:
        generator_env = {**generator_env, **lock.remotion_render_lock_child_env()}
        if installed:
            if not generator_origin:
                raise RemotionValidationError(
                    'installed registry generator carrier is unavailable: '
                    'registry discovery returned no generator origin'
                )
            generator_argv = _registry_origin.installed_argv(python, __file__, generate=True,
                                                            installed_source=installed_source)
            generator_env['ASTRID_HOST_EXECUTION_PROFILE'] = 'installed'
            generator_cwd = Path(tempfile.gettempdir()).resolve()
        else:
            generator_argv = [python, '-X', 'utf8', str(REPO_ROOT / 'scripts/gen_effect_registry.py')]
            generator_cwd = REPO_ROOT
        result, generator_wait = _owned_registry_probe(generator_argv,
                                                       cwd=generator_cwd, env=generator_env,
                                                       phase='registry_generator')
        _require_completed_probe(generator_wait, phase='registry_generator')
        if result.returncode:
            error = RemotionValidationError('owned registry generator failed')
            error.probe_receipt = generator_wait
            raise error
        expected_receipt = lock.REMOTION_LOCK_RECEIPT_PREFIX + generator_env[lock.REMOTION_LOCK_TOKEN_ENV]
        receipts = [line for line in result.stdout.splitlines()
                    if line.startswith(lock.REMOTION_LOCK_RECEIPT_PREFIX)]
        if len(receipts) != 1 or not secrets.compare_digest(receipts[0], expected_receipt):
            error = RemotionValidationError('owned registry generator did not return the lock receipt')
            error.probe_receipt = generator_wait
            raise error
        if run._registry_output_content_hashes(project) != expected['content_hashes']:
            raise RemotionValidationError('owned registry generator outputs differ from complete expected state')
        run._write_registry_state(project, expected)
    if run._read_registry_state(project) != expected or run._registry_output_content_hashes(project) != expected['content_hashes']:
        raise RemotionValidationError('settled registry four-file readback differs')
    bindings = [_file_binding(path) for path in (*output_paths, project / run._REGISTRY_STATE_PATH)]
    for item in probes.values():
        if _registry_inputs(item['observation']) != item['inputs']:
            raise RemotionValidationError('registry corpus changed during owned generator settling')
    for item in selected.values():
        item.update(outputs=bindings, expected_state=expected, generator_wait=generator_wait)
    return selected


@_validation_timing("launch_transaction")
def _prepare_remotion_validation(**kwargs: Any) -> dict[str, str]:
    from astrid.packs.rendering.rendering.renderers.remotion.lock import remotion_render_lock
    # Keep the existing lock continuously through all four-file readback,
    # owned validation, final bindings and exclusive readiness publication.
    with remotion_render_lock():
        return _prepare_remotion_validation_locked(**kwargs)


def _prepare_remotion_validation_locked(*, artifact_directory: Path, host_environment: Mapping[str, str],
                                python_executable: str, launch_id: str,
                                contexts: Mapping[str, Mapping[str, str] | list[Mapping[str, str]]],
                                installed_source: tuple[str, str] | None = None) -> dict[str, str]:
    """Launch-owner transaction for the content, schema and registry proof."""
    project_raw = host_environment.get(REMOTION_PROJECT_DIR_ENV, "")
    if not project_raw or not Path(project_raw).is_absolute():
        raise RemotionValidationError("launch validation requires the explicit Remotion project")
    if not re.fullmatch(r"[0-9a-f]{32}", launch_id) or not contexts:
        raise RemotionValidationError("runtime validation needs a fresh launch and explicit contexts")
    project = Path(project_raw).resolve(strict=True)
    from astrid.core.subprocess_env import build_child_subprocess_env
    # Rendering transport applies this same filter again. Qualify its exact
    # projection alongside the parent consumer; do not propagate loader keys.
    contexts = {role: [candidate for environment in ([values] if isinstance(values, Mapping) else values)
                       for candidate in (dict(environment), build_child_subprocess_env(base=environment, parent=environment))]
                for role, values in contexts.items()}
    registries = _settle_registries(project, contexts, python_executable,
        **({'installed_source': installed_source} if installed_source is not None else {}))
    entries: dict[str, Any] = {}
    validated: dict[bytes, dict[str, Any]] = {}
    # Registry selectors remain qualified independently. The owned Node and
    # isolated Python probes see only their exact projected environments.
    # Reuse is confined to this held-lock launch transaction and bracketed by
    # fresh complete content bindings, including one final publication fence.
    proofs: dict[bytes, dict[str, Any]] = {}
    proof_environments: dict[bytes, dict[str, str]] = {}
    digest_memo: dict = {}  # Never persisted or shared with consumers.
    def validate_context(environment: Mapping[str, str]) -> dict[str, Any]:
        context = probe_context(environment=environment, python_executable=python_executable)
        registry_context = _registry_context(environment, generator=False)
        registry_context[REMOTION_PROJECT_DIR_ENV] = str(project)
        context['registry_context'] = registry_context
        context['configuration'] = {k: environment.get(k) for k in
                                    (NODE_EXECUTABLE_ENV, REMOTION_PROJECT_DIR_ENV, TIMELINE_SCHEMA_PYTHONPATH_ENV)}
        key = _canonical(context)
        if key in validated:
            return validated[key]
        proof_key = _canonical({k: v for k, v in context.items() if k != 'registry_context'})
        proof = proofs.get(proof_key)
        if proof is not None:
            # The only environment inputs of _runtime_inputs (Node, schema path,
            # project) are in proof_key, so this context repeats a proven binding;
            # the final fresh per-proof check below still brackets publication.
            return qualify_registry(context, registry_context, environment, key, proof)
        schema_root = Path(environment[TIMELINE_SCHEMA_PYTHONPATH_ENV])
        schema_profile = 'installed' if _installed_registry_profile(environment) else 'source'
        node = Path(environment[NODE_EXECUTABLE_ENV]).resolve(strict=True)
        baseline = _runtime_inputs(project, environment, python_executable, {}, digest_memo=digest_memo)
        probe, node_wait = _owned_probe([str(node), '--version'], cwd=project,
                                       env=context['node_env'], timeout=5)
        version = probe.stdout.strip()
        if probe.returncode or not re.fullmatch(r'v\d+\.\d+\.\d+(?:[-+].*)?', version):
            error = RemotionValidationError("trusted Node version validation failed")
            error.probe_receipt = node_wait
            raise error
        schema_probe, schema_wait = _owned_probe(
            [python_executable, '-X', 'utf8', '-I', '-c', _schema_probe_source(), str(schema_root),
             str(REPO_ROOT), schema_profile],
            cwd=schema_root.parent, env=context['schema_env'], timeout=5)
        if schema_probe.returncode:
            error = RemotionValidationError("clean timeline schema validation failed")
            error.probe_receipt = schema_wait
            raise error
        try:
            closure = json.loads(schema_probe.stdout)
            if closure['origin'] != str(schema_root):
                raise RemotionValidationError("clean schema validation returned invalid identity")
            executable = closure['startup']['executable']
            if (not isinstance(executable, str) or not Path(executable).is_absolute()
                    or Path(executable).resolve(strict=True) != Path(python_executable).resolve(strict=True)):
                raise RemotionValidationError("schema probe reported a foreign interpreter")
            expected_prefix = Path(python_executable).parent.parent
            if ((expected_prefix / 'pyvenv.cfg').is_file()
                    and Path(closure['startup']['prefix']) != expected_prefix):
                raise RemotionValidationError("schema probe selected a foreign virtual environment")
        except (ValueError, KeyError, TypeError) as exc:
            error = RemotionValidationError("clean schema validation returned invalid identity")
            error.probe_receipt = schema_wait
            raise error from exc
        if _runtime_inputs(project, environment, python_executable, {}, digest_memo=digest_memo) != baseline:
            error = RemotionValidationError("runtime inputs changed during owned validation")
            error.probe_receipt = schema_wait
            raise error
        # Discovery is an owned transaction, but it is not the qualifying
        # import: establish its complete closure binding before that import.
        complete_before = _runtime_inputs(project, environment, python_executable, closure, digest_memo=digest_memo)
        qualifying, qualifying_wait = _owned_probe(
            [python_executable, '-X', 'utf8', '-I', '-c', _schema_probe_source(), str(schema_root),
             str(REPO_ROOT), schema_profile],
            cwd=schema_root.parent, env=context['schema_env'], timeout=5)
        try:
            qualifying_closure = json.loads(qualifying.stdout)
        except (ValueError, TypeError) as exc:
            error = RemotionValidationError('qualifying schema validation returned invalid closure')
            error.probe_receipt = qualifying_wait
            raise error from exc
        if (qualifying.returncode or qualifying_closure != closure
                or _runtime_inputs(project, environment, python_executable, closure, digest_memo=digest_memo) != complete_before):
            error = RemotionValidationError('schema dependency closure changed during qualifying validation')
            error.probe_receipt = qualifying_wait
            raise error
        proof = {'node_version': version, 'closure': closure, 'inputs': complete_before,
                 'original_waits': {'node': node_wait, 'schema_discovery': schema_wait,
                                    'schema': qualifying_wait}}
        proofs[proof_key] = proof
        proof_environments[proof_key] = dict(environment)
        return qualify_registry(context, registry_context, environment, key, proof)

    def qualify_registry(context, registry_context, environment, key, proof):
        registry_key = _canonical(registry_context)
        if registry_key not in registries:
            # A Python locale coercion is an observed explicit context. Requalify
            # its corpus rather than assuming the initial locale's inventory.
            registries.update(_settle_registries(project, {'transition': environment}, python_executable,
                                               expected_state=next(iter(registries.values())).get('expected_state')))
        entry = {**proof, 'context': context, 'registry': registries[registry_key]}
        validated[key] = entry
        return entry

    for role, environments in contexts.items():
        environments = (environments,) if isinstance(environments, Mapping) else environments
        for environment in environments:
            entry = validate_context(environment)
            variants = entries.setdefault(role, {'variants': []})['variants']
            if not any(v['context'] == entry['context'] for v in variants):
                variants.append(entry)
            observed = entry['closure']['startup']['locale_env']
            if observed != entry['context']['schema_env']:
                if (not isinstance(observed, dict) or set(observed) - set(_SCHEMA_ENV_KEYS)
                        or observed.get('HOME') != entry['context']['schema_env'].get('HOME')):
                    raise RemotionValidationError('schema startup reported an unsupported environment transition')
                transitioned_env = dict(environment)
                for name in _SCHEMA_ENV_KEYS:
                    transitioned_env.pop(name, None)
                transitioned_env.update(observed)
                transitioned = validate_context(transitioned_env)
                if (transitioned['node_version'] != entry['node_version']
                        or transitioned['closure']['startup']['locale_env'] != observed):
                    raise RemotionValidationError('schema locale transition did not validate')
                if not any(v['context'] == transitioned['context'] for v in variants):
                    variants.append(transitioned)
    for role in entries.values():
        for entry in role['variants']:
            for name, receipt in entry['original_waits'].items():
                phase = 'node_version' if name == 'node' else 'schema' if name == 'schema_discovery' else 'qualifying'
                _require_completed_probe(receipt, phase=phase)
    for registry in registries.values():
        if any(_file_binding(Path(binding['path'])) != binding for binding in registry['outputs']):
            raise RemotionValidationError('settled registry output changed before readiness publication')
        if registry['mode'] == 'source_settled':
            if registry['generator_wait'] is not None:
                _require_completed_probe(registry['generator_wait'], phase='registry_generator')
            for side in ('renderer',):
                for receipt in registry[side]['original_waits'].values():
                    _require_completed_probe(receipt, phase='registry_discovery')
                if _registry_inputs(registry[side]['observation']) != registry[side]['inputs']:
                    raise RemotionValidationError('settled corpus changed before readiness publication')
    for proof_key, proof in proofs.items():
        if _runtime_inputs(project, proof_environments[proof_key], python_executable, proof['closure'], digest_memo=digest_memo) != proof['inputs']:
            error = RemotionValidationError('schema dependency closure changed before readiness publication')
            error.probe_receipt = proof['original_waits']['schema']
            raise error
    artifact = {'schema': _VALIDATION_SCHEMA, 'launch_id': launch_id,
                'probe_source_sha256': hashlib.sha256(_schema_probe_source().encode()).hexdigest(),
                'contexts': entries, 'process_signal_authority': 'NONE'}
    artifact_directory.mkdir(parents=True, exist_ok=True)
    path = artifact_directory / ('remotion-validation-' + launch_id + '.json')
    raw = _canonical(artifact)
    if len(raw) > _VALIDATION_MAX_BYTES:
        raise RemotionValidationError('runtime validation artifact exceeds the bounded format')
    # Exclusively publish only after every original child wait and final census.
    with path.open('xb') as stream:
        platform.owner_only(stream.fileno())
        stream.write(raw)
    return {ASTRID_REMOTION_VALIDATION_PATH: str(path.absolute()),
            ASTRID_REMOTION_VALIDATION_SHA256: hashlib.sha256(raw).hexdigest(),
            ASTRID_REMOTION_VALIDATION_LAUNCH: launch_id,
            ASTRID_REMOTION_VALIDATION_ROLE: ('host' if 'host' in contexts else next(iter(contexts)))}



_UNRESOLVED_VALIDATION_PROBES: list[BaseException] = []


def prepare_remotion_validation(**kwargs: Any) -> dict[str, str]:
    """Explicit standalone/launch-owner validation, never an implicit fallback."""
    if _UNRESOLVED_PROBE_CUSTODY:
        raise _UNRESOLVED_PROBE_CUSTODY[0]
    if _UNRESOLVED_VALIDATION_PROBES:
        raise RemotionValidationError("prior runtime validation child custody is unresolved") from _UNRESOLVED_VALIDATION_PROBES[0]
    try:
        return _prepare_remotion_validation(**kwargs)
    except BaseException as error:
        if getattr(error, 'cleanup_error', None) is not None:
            _UNRESOLVED_VALIDATION_PROBES.append(error)
        record = {'schema': _VALIDATION_SCHEMA, 'status': 'FAILED_NOT_READINESS',
                  'error_type': type(error).__name__,
                  'original_probe_receipt': getattr(error, 'probe_receipt', None),
                  'probe_check': getattr(error, 'probe_check', None),
                  'retained_original_child_pid': getattr(getattr(error, 'process', None), 'pid', None),
                  'cleanup_uncertain': getattr(error, 'cleanup_error', None) is not None}
        try:
            directory = Path(kwargs['artifact_directory'])
            directory.mkdir(parents=True, exist_ok=True)
            evidence = directory / ('failed-validation-' + secrets.token_hex(16) + '.json')
            with evidence.open('xb') as stream:
                platform.owner_only(stream.fileno())
                stream.write(_canonical(record))
            error.validation_evidence = str(evidence)
        except BaseException as persistence:
            error.validation_evidence_error = type(persistence).__name__
        raise

def _validated_runtime(project_dir: Path, *, environment: Mapping[str, str] | None = None,
                       python_executable: str | None = None) -> dict[str, Any]:
    if _UNRESOLVED_PROBE_CUSTODY:
        raise _UNRESOLVED_PROBE_CUSTODY[0]
    env = os.environ if environment is None else environment
    python = sys.executable if python_executable is None else python_executable
    context = probe_context(environment=env, python_executable=python)
    registry_context = _registry_context(env, generator=False)
    registry_context[REMOTION_PROJECT_DIR_ENV] = str(project_dir)
    context['registry_context'] = registry_context
    context['configuration'] = {k: env.get(k) for k in
                                (NODE_EXECUTABLE_ENV, REMOTION_PROJECT_DIR_ENV, TIMELINE_SCHEMA_PYTHONPATH_ENV)}
    try:
        raw_path, digest, launch, role = (env[k] for k in _REF_ENV_KEYS)
        path = Path(raw_path)
        if not path.is_absolute() or path.is_symlink():
            raise RemotionValidationError("runtime validation reference is not a regular absolute file")
        st = path.stat()
        if not owned_by_current_user(path, st, mode=0o600):
            raise RemotionValidationError("runtime validation reference has a foreign owner/mode")
        if st.st_size > _VALIDATION_MAX_BYTES:
            raise RemotionValidationError("runtime validation artifact exceeds the bounded format")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RemotionValidationError("runtime validation artifact digest differs")
        artifact = json.loads(raw)
        if (artifact['schema'] != _VALIDATION_SCHEMA or artifact['launch_id'] != launch
                or artifact['probe_source_sha256'] != hashlib.sha256(_schema_probe_source().encode()).hexdigest()):
            raise RemotionValidationError("runtime validation launch/source differs")
        matches = [item for item in artifact['contexts'][role]['variants'] if item['context'] == context]
        if len(matches) != 1:
            raise RemotionValidationError(
                "runtime validation consumer context differs: this process's environment was not validated at "
                "launch (only the host and Remotion-capable capability workers are)")
        entry = matches[0]
        if python_executable is None:
            facts = entry['closure']['startup']
            current_facts = {'prefix': sys.prefix, 'exec_prefix': sys.exec_prefix,
                             'base_prefix': sys.base_prefix, 'base_exec_prefix': sys.base_exec_prefix,
                             'version': sys.version, 'cache_tag': sys.implementation.cache_tag,
                             'platform': sys.platform, 'abi_flags': getattr(sys, 'abiflags', '')}
            if any(facts.get(k) != value for k, value in current_facts.items()):
                raise RemotionValidationError('consumer interpreter installation differs')
        registry = entry['registry']
        if any(_file_binding(Path(binding['path'])) != binding for binding in registry['outputs']):
            raise RemotionValidationError('launch-bound generated registry outputs changed')
        if registry['mode'] == 'source_settled':
            for side in ('renderer',):
                current = _registry_inputs(registry[side]['observation'])
                if current != registry[side]['inputs']:
                    raise RemotionValidationError(
                        'launch-bound registry corpus changed: '
                        + _describe_corpus_change(registry[side]['inputs'], current))
        elif registry['state_file'] is not None and _file_binding(Path(registry['state_file']['path'])) != registry['state_file']:
            raise RemotionValidationError('provisioned registry state changed')
        if _runtime_inputs(project_dir, env, python, entry['closure']) != entry['inputs']:
            raise RemotionValidationError("runtime validation installed inputs changed")
        if any(r['original_wait_exit_code'] != 0 or not r['retained_set_final_census_complete']
               or not r['custody_admitted'] for r in entry['original_waits'].values()):
            raise RemotionValidationError("runtime validation lacks completed original probe custody")
        return entry
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise RemotionValidationError("missing or invalid launch validation; explicit owned validation required") from exc

def _configured_node_executable(environment: Mapping[str, str] | None = None) -> tuple[Path | None, str | None]:
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


def _probe_node(node_executable: Path, *, cwd: Path, environment: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """Bounded version probe for the trusted Node executable."""

    probe_env = _probe_environment(environment)
    try:
        probe, _receipt = _owned_probe(
            [str(node_executable), "--version"], cwd=cwd,
            env=probe_env, timeout=_NODE_PROBE_TIMEOUT_SECONDS,
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
    try:
        validation = _validated_runtime(project_dir, environment=environment)
    except RemotionProbeCleanupError:
        raise
    except (RemotionValidationError, OSError) as exc:
        return None, str(exc)
    node_version, reason = _probe_node(node_executable, cwd=project_dir, environment=environment)
    if reason:
        return None, reason
    if node_version != validation['node_version']:
        return None, 'trusted Node version differs from launch validation'
    assert node_version is not None
    from astrid.core.rendering.node_pin import node_pin

    pin = node_pin(project_dir)
    if pin and node_version != pin:
        return None, f"trusted Node version {node_version} does not match project pin {pin}"
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


def resolve_remotion_project_dir(*, environment: Mapping[str, str] | None = None) -> Path:
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
    missing = [f"@banodoco/{name}" for name in _REQUIRED_PACKAGES
               if not (node_modules / "@banodoco" / name / "package.json").is_file()]
    if missing:
        return "required Remotion package(s) missing: " + ", ".join(missing) + f"; {fix}"
    if not (project / REMOTION_CLI_RELATIVE_PATH).is_file():
        return f"locked Remotion CLI is missing: {project / REMOTION_CLI_RELATIVE_PATH}; {fix}"
    composition_problem = _composition_package_problem(project)
    if composition_problem:
        return f"{composition_problem}; {fix}"
    return None


def remotion_runtime_status(
    *, require_explicit_project: bool = False, environment: Mapping[str, str] | None = None
) -> RemotionRuntimeStatus:
    """Inspect the trusted runtime without mutating it or running npm.

    Development render helpers may use the checkout's conventional ``remotion``
    directory, but admission and worker execution must use an explicitly
    provisioned server-owned project. The strict mode makes that distinction
    explicit without weakening the development parity lane.
    """

    source = os.environ if environment is None else environment
    if _UNRESOLVED_PROBE_CUSTODY:
        raise _UNRESOLVED_PROBE_CUSTODY[0]
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
        project_dir = resolve_remotion_project_dir(environment=environment)
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
    try:
        _local_schema.verified_schema_path(REPO_ROOT, schema_root_raw)
    except (OSError, ValueError) as exc:
        return RemotionRuntimeStatus(
            False,
            project_dir,
            f"installed timeline schema is incomplete or invalid: {exc}",
        )
    runtime_tools, tools_error = resolve_remotion_runtime_tools(project_dir, environment=environment)
    if tools_error:
        return RemotionRuntimeStatus(
            False,
            project_dir,
            tools_error,
        )
    assert runtime_tools is not None
    # Resolver has already verified the current launch-bound schema validation.
    return RemotionRuntimeStatus(
        True,
        project_dir,
        node_executable=runtime_tools.node_executable,
        node_version=runtime_tools.node_version,
        remotion_cli=runtime_tools.remotion_cli,
    )


@contextmanager
def prepared_scene_root(*args: Any, **kwargs: Any) -> Iterator[Any]:
    """Yield the existing prepared Root under the renderer's shared lock.

    Scene observers use this neutral runtime seam so they do not import a
    concrete renderer. The backend preparation context remains the single
    owner of Root admission and cleanup; this helper only preserves its
    existing lock-and-lifetime boundary.
    """
    from astrid.packs.rendering.rendering.renderers.remotion import lock
    from astrid.packs.rendering.rendering.renderers.remotion.root_preparation import (
        _prepared_remotion_root,
    )

    with lock.remotion_render_lock(), _prepared_remotion_root(*args, **kwargs) as prepared:
        yield prepared


def default_composition_id() -> str:
    """Return the backend-owned default without exposing the backend to callers."""
    from astrid.packs.rendering.rendering.renderers.remotion.run import (
        DEFAULT_COMPOSITION_ID,
    )

    return DEFAULT_COMPOSITION_ID


__all__ = [
    "NODE_EXECUTABLE_ENV",
    "REMOTION_CLI_RELATIVE_PATH",
    "REMOTION_PROJECT_DIR_ENV",
    "RemotionRuntimeTools",
    "TIMELINE_SCHEMA_PYTHONPATH_ENV",
    "RemotionRuntimeStatus",
    "default_composition_id",
    "prepared_scene_root",
    "resolve_remotion_project_dir",
    "resolve_remotion_runtime_tools",
    "remotion_install_problem",
    "remotion_runtime_status",
    "timeline_requires_remotion",
]
