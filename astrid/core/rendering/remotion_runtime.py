"""Server-owned Remotion runtime discovery for Astrid render admission.

The Python package does not contain a Node checkout.  A release deployment
must point ``ASTRID_REMOTION_PROJECT_DIR`` at its separately provisioned,
server-owned Remotion bundle.  No task/request value is ever consulted here.
"""

from __future__ import annotations

import os
import hashlib
import json
import secrets
import stat
import re
import subprocess
import sys
import time
from functools import wraps
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from astrid.core.env_vars import (
    ASTRID_NODE_EXECUTABLE,
    ASTRID_PACKS_PATH,
    ASTRID_REMOTION_VALIDATION_PATH,
    ASTRID_REMOTION_VALIDATION_SHA256,
    ASTRID_REMOTION_VALIDATION_LAUNCH,
    ASTRID_REMOTION_VALIDATION_ROLE,

    ASTRID_REMOTION_PROJECT_DIR,
    ASTRID_TIMELINE_SCHEMA_PYTHONPATH,
)
from astrid.core.foundation.paths import REPO_ROOT

REMOTION_PROJECT_DIR_ENV = ASTRID_REMOTION_PROJECT_DIR
NODE_EXECUTABLE_ENV = ASTRID_NODE_EXECUTABLE
TIMELINE_SCHEMA_PYTHONPATH_ENV = ASTRID_TIMELINE_SCHEMA_PYTHONPATH
REMOTION_CLI_RELATIVE_PATH = Path("node_modules/@remotion/cli/remotion-cli.js")
_NODE_PROBE_TIMEOUT_SECONDS = 5
_REGISTRY_PROBE_TIMEOUT_SECONDS = 90
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
    load_schema,
    materialize_output,
    resolve_theme,
    validate_timeline,
)
origin = pathlib.Path(schema.__file__).resolve()
if not origin.is_relative_to(root / "banodoco_timeline_schema"):
    raise RuntimeError(f"schema origin escaped trusted root: {origin}")
load_schema()
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


_VALIDATION_PHASES = frozenset({"launch_transaction", "registry_settle", "context_binding", "complete_inputs",
                               "owned_probe", "owned_probe_acquire", "owned_probe_execute"})


def _validation_error_phase(error):
    phase = getattr(error, "validation_phase", None)
    return phase if isinstance(phase, str) and phase in _VALIDATION_PHASES else None


def _validation_timing(phase: str):
    """Bounded diagnostic metadata; no launch paths, arguments or environment."""
    phase = phase if isinstance(phase, str) and phase in _VALIDATION_PHASES else "unknown"
    def decorate(operation):
        @wraps(operation)
        def measured(*args, **kwargs):
            started = time.monotonic()
            outcome = "failed"
            try:
                print(json.dumps({"diagnostic": "astrid.remotion_validation_phase", "phase": phase,
                    "outcome": "begun", "process_id": os.getpid(), "elapsed_ms": 0}), file=sys.stderr, flush=True)
            except BaseException:
                pass
            try:
                result = operation(*args, **kwargs)
                outcome = "completed"
                return result
            except BaseException as error:
                try:
                    if not hasattr(error, "validation_phase"):
                        error.validation_phase = phase
                except BaseException:
                    pass
                raise
            finally:
                try:
                    print(json.dumps({"diagnostic": "astrid.remotion_validation_phase",
                                      "phase": phase, "outcome": outcome,
                                      "process_id": os.getpid(),
                                      "elapsed_ms": round((time.monotonic() - started) * 1000)}),
                          file=sys.stderr, flush=True)
                except BaseException:
                    pass  # Optional diagnostics never replace validation failures.
        return measured
    return decorate


@_validation_timing("owned_probe")
def _owned_probe(argv: list[str], *, cwd: Path, env: Mapping[str, str],
                 timeout: float) -> tuple[subprocess.CompletedProcess, dict[str, Any]]:
    from astrid.core.execution.process_group import (
        popen_owned_group, terminate_group, verify_tree_absent,
    )

    # The existing launch primitive treats an empty mapping as ambient env.
    if not env:
        raise RemotionValidationError("owned probe needs an explicit nonempty environment")
    process = None
    try:
        process = _validation_timing("owned_probe_acquire")(popen_owned_group)(
            argv, cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        @_validation_timing("owned_probe_execute")
        def execute():
            stdout, stderr = process.communicate(timeout=timeout)
            code = process.wait(timeout=0)
            verify_tree_absent(process)
            return stdout, stderr, code
        stdout, stderr, code = execute()
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
                raise primary from cleanup
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
_SCHEMA_ENV_KEYS = ("HOME", "LANG", "LC_ALL", "LC_CTYPE")
_REF_ENV_KEYS = (ASTRID_REMOTION_VALIDATION_PATH, ASTRID_REMOTION_VALIDATION_SHA256,
                 ASTRID_REMOTION_VALIDATION_LAUNCH, ASTRID_REMOTION_VALIDATION_ROLE)


def probe_context(*, environment: Mapping[str, str], python_executable: str) -> dict[str, Any]:
    """Project the documented effective probes; never copy task secrets."""
    home = environment.get("HOME")
    if not isinstance(home, str) or not home or not Path(home).is_absolute():
        raise RemotionValidationError("runtime validation needs an explicit absolute HOME")
    forbidden = {k for k in environment if k.startswith(("DYLD_", "LD_"))}
    forbidden.update(k for k in ("PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__", "PYTHONHOME",
                                "NODE_OPTIONS", "NODE_PATH", "NODE_ICU_DATA", "OPENSSL_CONF")
                     if k in environment)
    forbidden.update(k for k in _UNSUPPORTED_PYTHON_STARTUP if k in environment)
    if forbidden:
        raise RemotionValidationError("runtime validation refuses unsupported loader/interpreter overrides")
    python = Path(python_executable)
    if not python.is_absolute():
        raise RemotionValidationError("runtime validation needs an absolute lexical interpreter")
    if any(not isinstance(environment[k], str) for k in _NODE_ENV_KEYS if k in environment):
        raise RemotionValidationError("runtime probe environment values must be strings")
    return {"policy": "clean-schema-env/v1", "python_executable": str(python),
            "node_env": {k: environment[k] for k in _NODE_ENV_KEYS if k in environment},
            "schema_env": {k: environment[k] for k in _SCHEMA_ENV_KEYS if k in environment}}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _file_binding(path: Path, *, digest_memo: dict | None = None) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise RemotionValidationError("runtime validation input is not a file")
    before = resolved.stat()
    def identity(facts):
        return (facts.st_dev, facts.st_ino, facts.st_size, facts.st_mtime_ns,
                facts.st_ctime_ns, facts.st_mode, facts.st_uid)
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
            if path.name in {"__pycache__", ".cache", ".git"} or path.suffix == ".pyc":
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
    schema = Path(schema_raw).resolve(strict=True)
    cli = project / REMOTION_CLI_RELATIVE_PATH
    cli.resolve(strict=True).relative_to(project)
    for name in _REQUIRED_PACKAGES:
        if not (project / "node_modules" / "@banodoco" / name / "package.json").is_file():
            raise RemotionValidationError("installed Remotion package is missing")
    for name in _REQUIRED_SCHEMA_FILES:
        if not (schema / "banodoco_timeline_schema" / name).is_file():
            raise RemotionValidationError("installed timeline schema is incomplete")
    roots = [project / "node_modules", schema / "banodoco_timeline_schema"]
    if (project / "src").is_dir():
        roots.append(project / "src")
    roots.extend(Path(p) for p in closure.get("package_roots", []))
    paths = [node, Path(python), cli, project / "package.json", project / "package-lock.json", Path(__file__)]
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
        startup[raw] = sorted(p.name for p in directory.iterdir() if p.name != "__pycache__")
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
                             'locale_env': {k: os.environ[k] for k in ('HOME','LANG','LC_ALL','LC_CTYPE') if k in os.environ},
                             'filesystem_encoding': sys.getfilesystemencoding(),
                             'stdout_encoding': sys.stdout.encoding}}))
""")


_REGISTRY_KINDS = ('effects', 'animations', 'transitions')
_REGISTRY_SELECTORS = ('ASTRID_PACKS_PATH', 'ASTRID_SOURCE_STATE', 'XDG_STATE_HOME')


def _selected_source_state_binding(environment: Mapping[str, str]) -> dict[str, Any] | None:
    """Bind an explicit launch selection; absence alone keeps legacy discovery."""
    from astrid.core.pack.source_setup import SourceSetupError, load_source_state
    if 'ASTRID_SOURCE_STATE' not in environment:
        return None
    raw = environment['ASTRID_SOURCE_STATE']
    try:
        if not isinstance(raw, str) or not raw or raw != raw.strip() or "\0" in raw:
            raise ValueError('invalid selector')
        path = Path(raw)
        if (not path.is_absolute() or str(path) != raw or path.resolve(strict=True) != path
                or not path.is_file() or path.stat().st_nlink != 1):
            raise ValueError('selected state must be an ordinary non-aliased file')
        before = _file_binding(path)
        load_source_state(path)
        if _file_binding(path) != before:
            raise ValueError('selected state changed while reading')
        return before
    except (OSError, ValueError, SourceSetupError) as exc:
        raise RemotionValidationError('selected source state is missing, invalid or aliased') from exc


def _registry_context(environment: Mapping[str, str], *, generator: bool) -> dict[str, str]:
    from astrid.core.subprocess_env import _SAFE_BASE_ENV
    probe_context(environment=environment, python_executable=sys.executable)
    if any(k.startswith('GIT_') or k in {'PYTHONSTARTUP'}
           for k in environment):
        # Git/startup overrides are outside this selected launch contract.
        raise RemotionValidationError('registry validation refuses unsupported startup/Git overrides')
    for name in ('ASTRID_SOURCE_STATE', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME'):
        if environment.get(name) and not Path(environment[name]).is_absolute():
            raise RemotionValidationError('registry selectors require absolute launch-known paths')
    if environment.get('PYTHONPATH') and any(not raw or not Path(raw).is_absolute() for raw in environment['PYTHONPATH'].split(os.pathsep)):
        raise RemotionValidationError('registry import paths require absolute launch-known locations')
    _selected_source_state_binding(environment)
    keys = set(_SAFE_BASE_ENV) | {'ASTRID_PACKS_PATH', 'ASTRID_SOURCE_STATE', TIMELINE_SCHEMA_PYTHONPATH_ENV}
    if not generator:
        keys.update((*_REGISTRY_SELECTORS, 'PYTHONPATH', 'XDG_CONFIG_HOME'))
    return {k: environment[k] for k in sorted(keys) if k in environment}


def _registry_config_files(environment: Mapping[str, str]) -> list[Path]:
    import shutil
    home = Path(environment['HOME'])
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
    from astrid.packs.rendering.rendering.renderers.remotion import lock
    selected_state = _selected_source_state_binding(os.environ)
    config_files = _registry_config_files(os.environ)
    _refuse_git_config_indirection(config_files)
    managed_git_roots, managed_config_files = _managed_registry_configuration()
    clear_default_registry_cache()
    generator = importlib.import_module('scripts.gen_effect_registry')
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
    if _selected_source_state_binding(os.environ) != selected_state:
        raise RemotionValidationError('selected source state changed during registry discovery')
    return {'state': state, 'roots': roots, 'optional_files': optional_files,
            'selected_source_state': selected_state,
            'module_files': files, 'package_roots': package_roots,
            'search_paths': [str(Path(value or '.').absolute()) for value in sys.path],
            'search_path_spellings': sys.path, 'probe_cwd': os.getcwd(),
            'generator_origin': str(Path(generator.__file__).resolve()),
            'astrid_origin': str(Path(astrid.__file__).resolve()),
            'runtime_origin': str(Path(__file__).resolve()),
            'managed_inventory_identity': inventory.identity,
            'lock_path': str(lock._resolved_lock_path()),
            'startup': {'executable': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix}}


def _registry_inputs(observation: Mapping[str, Any]) -> dict[str, Any]:
    selected = observation.get('selected_source_state')
    if selected is not None:
        current = _selected_source_state_binding({'ASTRID_SOURCE_STATE': selected['path']})
        if current != selected:
            raise RemotionValidationError('selected source state changed after registry discovery')
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
            inventory['startup:' + raw] = sorted(p.name for p in root.iterdir() if p.name != '__pycache__')
            paths.extend(root.glob('*.pth'))
            for name in ('sitecustomize.py', 'sitecustomize.pyc', 'usercustomize.py', 'usercustomize.pyc'):
                if (root / name).is_file():
                    paths.append(root / name)
    unique = {str(path.absolute()): path for path in paths}
    bindings = [_file_binding(unique[key]) for key in sorted(unique)]
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


def _registry_probe(environment: Mapping[str, str], python: str, *, generator: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    from astrid.packs.rendering.rendering.renderers.remotion import run
    script_directory = REPO_ROOT / 'scripts' if generator else Path(run.__file__).resolve().parents[2]
    # Match both real script launchers: replace -c's cwd search entry with
    # the script directory, and insert checkout only when not already present.
    source = ('import sys,json; sys.path[0]=sys.argv[2]; '
              'sys.path.insert(0,sys.argv[1]) if sys.argv[1] not in sys.path else None; '
              'from astrid.core.rendering.remotion_runtime import _registry_observation; '
              'print(json.dumps(_registry_observation(),sort_keys=True))')
    cwd = REPO_ROOT if generator else script_directory
    result, receipt = _owned_registry_probe([python, '-c', source, str(REPO_ROOT), str(script_directory)], cwd=cwd,
                                           env=environment, phase='registry_discovery')
    _require_completed_probe(receipt, phase='registry_discovery')
    try:
        observation = json.loads(result.stdout)
        if (result.returncode or observation['runtime_origin'] != str(Path(__file__).resolve())
                or observation['startup']['executable'] != python):
            raise ValueError('registry source/interpreter differs')
        hashes = observation['state']['content_hashes']
        if (set(hashes) != set(_REGISTRY_KINDS) or not observation['state'].get('hash')
                or any(not re.fullmatch('[0-9a-f]{64}', value) for value in hashes.values())):
            raise ValueError('registry state is incomplete')
    except (ValueError, KeyError, TypeError) as exc:
        error = RemotionValidationError('owned registry discovery failed')
        error.probe_receipt = receipt
        raise error from exc
    return observation, receipt


@_validation_timing("registry_settle")
def _settle_registries(project: Path, contexts: Mapping[str, Any], python: str, *, expected_state: Any = None) -> dict[bytes, Any]:
    from astrid.packs.rendering.rendering.renderers.remotion import run, lock
    if run._timeline_composition_src(project) is None:
        raise RemotionValidationError('launch validation needs the installed composition output directory')
    output_paths = run._registry_output_paths(project)
    selected: dict[bytes, Any] = {}
    if run.gen_effect_registry is None:
        # Installed wheel: only the provisioned executable outputs are claimed.
        for paths in contexts.values():
            for environment in ([paths] if isinstance(paths, Mapping) else paths):
                context = _registry_context(environment, generator=False)
                context['ASTRID_TIMELINE_COMPOSITION_SRC'] = str(run._timeline_composition_src(project))
                key = _canonical(context)
                selected[key] = {'mode': 'provisioned_only', 'outputs': [_file_binding(p) for p in output_paths],
                                 'selected_source_state': _selected_source_state_binding(context),
                                 'state_file': _file_binding(project / run._REGISTRY_STATE_PATH)
                                 if (project / run._REGISTRY_STATE_PATH).exists() else None}
        return selected
    if not lock.remotion_render_lock_held():
        raise RemotionValidationError('registry settling requires the original held Remotion lock')
    probes: dict[bytes, Any] = {}
    expected = expected_state
    generator_env = None
    for paths in contexts.values():
        for environment in ([paths] if isinstance(paths, Mapping) else paths):
            renderer = _registry_context(environment, generator=False)
            generator = _registry_context(environment, generator=True)
            generator['ASTRID_TIMELINE_COMPOSITION_SRC'] = str(run._timeline_composition_src(project))
            for is_generator, current in ((False, renderer), (True, generator)):
                # Destination is selected at generator import in both contexts.
                current['ASTRID_TIMELINE_COMPOSITION_SRC'] = str(run._timeline_composition_src(project))
                key = _canonical({'environment': current, 'generator_startup': is_generator})
                if key not in probes:
                    first, discovery_wait = _registry_probe(current, python, generator=is_generator)
                    _require_completed_probe(discovery_wait, phase='registry_discovery')
                    bound = _registry_inputs(first)
                    second, qualifying_wait = _registry_probe(current, python, generator=is_generator)
                    _require_completed_probe(qualifying_wait, phase='registry_discovery')
                    if first != second or _registry_inputs(second) != bound:
                        raise RemotionValidationError('registry corpus changed during owned qualifying discovery')
                    if first['lock_path'] != str(lock._resolved_lock_path()):
                        raise RemotionValidationError('registry generator selected another Remotion lock')
                    probes[key] = {'observation': second, 'inputs': bound,
                                   'original_waits': {'discovery': discovery_wait, 'qualifying': qualifying_wait}}
                state = probes[key]['observation']['state']
                if expected is None:
                    expected = state
                if state != expected:
                    raise RemotionValidationError('renderer/generator sharing contexts expect different registry contents')
            selected[_canonical(renderer)] = {'mode': 'source_settled', 'renderer': probes[_canonical({'environment': renderer, 'generator_startup': False})],
                                               'generator': probes[_canonical({'environment': generator, 'generator_startup': True})]}
            generator_env = generator
    cached = run._read_registry_state(project)  # Fresh parse; never trust an LRU.
    current_hashes = run._registry_output_content_hashes(project)
    generator_wait = None
    if cached != expected or current_hashes != expected['content_hashes']:
        generator_env = {**generator_env, **lock.remotion_render_lock_child_env()}
        result, generator_wait = _owned_registry_probe([python, str(REPO_ROOT / 'scripts/gen_effect_registry.py')],
                                                      cwd=REPO_ROOT, env=generator_env, phase='registry_generator')
        _require_completed_probe(generator_wait, phase='registry_generator')
        if result.returncode:
            error = RemotionValidationError('owned registry generator failed')
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
    from astrid.packs.rendering.rendering.renderers.remotion import run, lock
    # Provisioned wheel registries are read-only. Their complete bindings and
    # final publication fence need no writer lock, which would mutate the
    # authenticated installed package tree. No source writer uses this lane.
    if run.gen_effect_registry is None:
        return _prepare_remotion_validation_locked(**kwargs)
    # Source generators retain the shared writer lock continuously through
    # readback, owned validation, final bindings and exclusive publication.
    with lock.remotion_render_lock():
        return _prepare_remotion_validation_locked(**kwargs)


def _prepare_remotion_validation_locked(*, artifact_directory: Path, host_environment: Mapping[str, str],
                                python_executable: str, launch_id: str,
                                contexts: Mapping[str, Mapping[str, str] | list[Mapping[str, str]]]) -> dict[str, str]:
    """Explicit launch-owner transaction; ordinary resolver calls never spawn."""
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
    registries = _settle_registries(project, contexts, python_executable)
    entries: dict[str, Any] = {}
    validated: dict[bytes, dict[str, Any]] = {}
    # Registry selectors remain qualified independently. The owned Node and
    # isolated Python probes see only their exact projected environments.
    # Reuse is confined to this held-lock launch transaction and bracketed by
    # fresh complete content bindings, including one final publication fence.
    proofs: dict[bytes, dict[str, Any]] = {}
    proof_environments: dict[bytes, dict[str, str]] = {}
    digest_memo: dict = {}  # Never persisted or shared with consumers.
    @_validation_timing("context_binding")
    def validate_context(environment: Mapping[str, str]) -> dict[str, Any]:
        context = probe_context(environment=environment, python_executable=python_executable)
        registry_context = _registry_context(environment, generator=False)
        registry_context['ASTRID_TIMELINE_COMPOSITION_SRC'] = str(project / 'node_modules/@banodoco/timeline-composition/typescript/src')
        context['registry_context'] = registry_context
        context['configuration'] = {k: environment.get(k) for k in
                                    (NODE_EXECUTABLE_ENV, REMOTION_PROJECT_DIR_ENV, TIMELINE_SCHEMA_PYTHONPATH_ENV)}
        key = _canonical(context)
        if key in validated:
            return validated[key]
        proof_key = _canonical({k: v for k, v in context.items() if k != 'registry_context'})
        proof = proofs.get(proof_key)
        if proof is not None:
            if _runtime_inputs(project, environment, python_executable, proof['closure'], digest_memo=digest_memo) != proof['inputs']:
                error = RemotionValidationError('schema dependency closure changed between launch contexts')
                error.probe_receipt = proof['original_waits']['schema']
                raise error
            return qualify_registry(context, registry_context, environment, key, proof)
        schema_root = Path(environment[TIMELINE_SCHEMA_PYTHONPATH_ENV]).resolve(strict=True)
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
            [python_executable, '-I', '-B', '-c', _schema_probe_source(), str(schema_root)],
            cwd=schema_root.parent, env=context['schema_env'], timeout=5)
        if schema_probe.returncode:
            error = RemotionValidationError("clean timeline schema validation failed")
            error.probe_receipt = schema_wait
            raise error
        try:
            closure = json.loads(schema_probe.stdout)
            Path(closure['origin']).relative_to(schema_root / 'banodoco_timeline_schema')
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
            [python_executable, '-I', '-B', '-c', _schema_probe_source(), str(schema_root)],
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
                        or observed.get('HOME') != entry['context']['schema_env']['HOME']):
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
        selected = registry.get('selected_source_state')
        if selected is not None and _selected_source_state_binding({'ASTRID_SOURCE_STATE': selected['path']}) != selected:
            raise RemotionValidationError('selected source state changed before readiness publication')
        if any(_file_binding(Path(binding['path'])) != binding for binding in registry['outputs']):
            raise RemotionValidationError('settled registry output changed before readiness publication')
        if registry['mode'] == 'source_settled':
            if registry['generator_wait'] is not None:
                _require_completed_probe(registry['generator_wait'], phase='registry_generator')
            for side in ('renderer', 'generator'):
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
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
    return {ASTRID_REMOTION_VALIDATION_PATH: str(path.absolute()),
            ASTRID_REMOTION_VALIDATION_SHA256: hashlib.sha256(raw).hexdigest(),
            ASTRID_REMOTION_VALIDATION_LAUNCH: launch_id,
            ASTRID_REMOTION_VALIDATION_ROLE: ('host' if 'host' in contexts else next(iter(contexts)))}



_UNRESOLVED_VALIDATION_PROBES: list[BaseException] = []


def prepare_remotion_validation(**kwargs: Any) -> dict[str, str]:
    """Explicit standalone/launch-owner validation, never an implicit fallback."""
    if _UNRESOLVED_VALIDATION_PROBES:
        raise RemotionValidationError("prior runtime validation child custody is unresolved") from _UNRESOLVED_VALIDATION_PROBES[0]
    try:
        return _prepare_remotion_validation(**kwargs)
    except BaseException as error:
        if getattr(error, 'cleanup_error', None) is not None:
            _UNRESOLVED_VALIDATION_PROBES.append(error)
        record = {'schema': _VALIDATION_SCHEMA, 'status': 'FAILED_NOT_READINESS',
                  'error_type': type(error).__name__,
                  'last_begun_phase': _validation_error_phase(error),
                  'original_probe_receipt': getattr(error, 'probe_receipt', None),
                  'probe_check': getattr(error, 'probe_check', None),
                  'retained_original_child_pid': getattr(getattr(error, 'process', None), 'pid', None),
                  'cleanup_uncertain': getattr(error, 'cleanup_error', None) is not None}
        try:
            directory = Path(kwargs['artifact_directory'])
            directory.mkdir(parents=True, exist_ok=True)
            evidence = directory / ('failed-validation-' + secrets.token_hex(16) + '.json')
            with evidence.open('xb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(_canonical(record))
            error.validation_evidence = str(evidence)
        except BaseException as persistence:
            error.validation_evidence_error = type(persistence).__name__
        try:
            print(json.dumps({'diagnostic': 'astrid.remotion_validation_failure',
                'error_type': type(error).__name__[:80],
                'last_begun_phase': _validation_error_phase(error),
                'persistence_error_class': getattr(error, 'validation_evidence_error', None)}),
                file=sys.stderr, flush=True)
        except BaseException:
            pass
        raise

def _observed_consumer_startup():
    return {'prefix': sys.prefix, 'exec_prefix': sys.exec_prefix,
            'base_prefix': sys.base_prefix, 'base_exec_prefix': sys.base_exec_prefix,
            'version': sys.version, 'cache_tag': sys.implementation.cache_tag,
            'platform': sys.platform, 'abi_flags': getattr(sys, 'abiflags', '')}


def _consumer_uses_no_site():
    return bool(sys.flags.no_site)


def _consumer_startup_facts(expected):
    current = _observed_consumer_startup()
    if current == expected or not _consumer_uses_no_site():
        return current
    # Python3.11 -S reports base prefixes although the retained launcher maps
    # ONLY the independently verified installed environment. Do not enable site,
    # infer a venv from ambient paths, or replace any other interpreter facts.
    from astrid.core.execution import generic_host
    boundary = generic_host._retained_python_boundary()
    if boundary is None:
        return current
    selected = generic_host.__dict__.get('_RETAINED_SELECTED_PYTHON', {})
    prefix, exec_prefix = expected.get('prefix'), expected.get('exec_prefix')
    if (not isinstance(prefix, str) or prefix != exec_prefix or not Path(prefix).is_absolute()
            or '..' in Path(prefix).parts
            or current['prefix'] != current['base_prefix']
            or current['exec_prefix'] != current['base_exec_prefix']
            or not boundary[0]['installed-packages'].is_relative_to(Path(prefix))):
        return current
    cfg = Path(prefix) / 'pyvenv.cfg'
    pin = selected.get('startup_files', {}).get(str(cfg))
    if not isinstance(pin, dict) or not isinstance(pin.get('sha256'), str):
        return current
    resolved = cfg.resolve(strict=True)
    if not any(resolved.is_relative_to(root) for root in (Path(prefix), boundary[0]['python-runtime'])):
        return current
    from runtime_protocol.verified_host_launch import _local_read
    try:
        _local_read(cfg, pin['sha256'])  # Same bounded64MiB installed reader.
    except (OSError, RuntimeError, ValueError):
        return current
    current.update(prefix=prefix, exec_prefix=exec_prefix)
    return current


def _validated_runtime(project_dir: Path, *, environment: Mapping[str, str] | None = None,
                       python_executable: str | None = None) -> dict[str, Any]:
    env = os.environ if environment is None else environment
    python = sys.executable if python_executable is None else python_executable
    context = probe_context(environment=env, python_executable=python)
    registry_context = _registry_context(env, generator=False)
    registry_context['ASTRID_TIMELINE_COMPOSITION_SRC'] = str(project_dir / 'node_modules/@banodoco/timeline-composition/typescript/src')
    context['registry_context'] = registry_context
    context['configuration'] = {k: env.get(k) for k in
                                (NODE_EXECUTABLE_ENV, REMOTION_PROJECT_DIR_ENV, TIMELINE_SCHEMA_PYTHONPATH_ENV)}
    try:
        raw_path, digest, launch, role = (env[k] for k in _REF_ENV_KEYS)
        path = Path(raw_path)
        if not path.is_absolute() or path.is_symlink():
            raise RemotionValidationError("runtime validation reference is not a regular absolute file")
        st = path.stat()
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
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
            raise RemotionValidationError("runtime validation consumer context differs")
        entry = matches[0]
        if python_executable is None:
            facts = entry['closure']['startup']
            current_facts = _consumer_startup_facts(facts)
            if any(facts.get(k) != value for k, value in current_facts.items()):
                raise RemotionValidationError('consumer interpreter installation differs')
        registry = entry['registry']
        if registry.get('selected_source_state') != _selected_source_state_binding(env) and registry['mode'] == 'provisioned_only':
            raise RemotionValidationError('launch-bound selected source state changed')
        if any(_file_binding(Path(binding['path'])) != binding for binding in registry['outputs']):
            raise RemotionValidationError('launch-bound generated registry outputs changed')
        if registry['mode'] == 'source_settled':
            for side in ('renderer', 'generator'):
                if _registry_inputs(registry[side]['observation']) != registry[side]['inputs']:
                    raise RemotionValidationError('launch-bound registry corpus changed')
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

def _configured_node_executable() -> tuple[Path | None, str | None]:
    raw = os.environ.get(NODE_EXECUTABLE_ENV, "").strip()
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


def _probe_node(node_executable: Path, *, cwd: Path) -> tuple[str | None, str | None]:
    """Bounded version probe for the trusted Node executable."""

    probe_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR"}
    }
    try:
        probe, _receipt = _owned_probe(
            [str(node_executable), "--version"], cwd=cwd,
            env=probe_env, timeout=_NODE_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, RemotionValidationError) as exc:
        return None, f"trusted Node version probe failed: {exc}"
    version = probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else ""
    if probe.returncode != 0 or not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+].*)?", version):
        detail = probe.stderr.strip().splitlines()[-1:] or ["invalid version output"]
        return None, f"trusted Node version probe failed: {detail[0]}"
    return version, None


def resolve_remotion_runtime_tools(
    project_dir: Path,
) -> tuple[RemotionRuntimeTools | None, str | None]:
    """Resolve and validate the server-owned Node + locked local CLI pair."""

    node_executable, reason = _configured_node_executable()
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
        validation = _validated_runtime(project_dir)
    except (RemotionValidationError, OSError) as exc:
        return None, str(exc)
    node_version = validation['node_version']
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


def resolve_remotion_project_dir() -> Path:
    """Resolve the server-owned project, with a source-checkout development fallback.

    Only process configuration is consulted; renderer requests cannot select a
    runtime. Invalid explicit configuration must never fall back to the checkout.
    """
    raw = os.environ.get(REMOTION_PROJECT_DIR_ENV, "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{REMOTION_PROJECT_DIR_ENV} must be an absolute path")
        return candidate.resolve()
    # Development source checkouts may use the conventional location.  A
    # wheel install has no sibling remotion/ tree, so this correctly fails
    # closed until deployment supplies the trusted runtime configuration.
    return (REPO_ROOT / "remotion").resolve()


def remotion_runtime_status(
    *, require_explicit_project: bool = False
) -> RemotionRuntimeStatus:
    """Inspect the trusted runtime without mutating it or running npm.

    Development render helpers may use the checkout's conventional ``remotion``
    directory, but admission and worker execution must use an explicitly
    provisioned server-owned project. The strict mode makes that distinction
    explicit without weakening the development parity lane.
    """

    if require_explicit_project and not os.environ.get(REMOTION_PROJECT_DIR_ENV, "").strip():
        return RemotionRuntimeStatus(
            False,
            None,
            f"{REMOTION_PROJECT_DIR_ENV} must point to the server-owned Remotion project",
        )

    try:
        project_dir = resolve_remotion_project_dir()
    except ValueError as exc:
        return RemotionRuntimeStatus(False, None, str(exc))
    if not project_dir.is_dir():
        return RemotionRuntimeStatus(False, project_dir, f"project directory not found: {project_dir}")
    if not (project_dir / "package.json").is_file():
        return RemotionRuntimeStatus(False, project_dir, f"package.json missing: {project_dir}")
    node_modules = project_dir / "node_modules"
    if not node_modules.is_dir():
        return RemotionRuntimeStatus(False, project_dir, f"node_modules missing: {node_modules}")
    missing = [
        f"@banodoco/{name}"
        for name in _REQUIRED_PACKAGES
        if not (node_modules / "@banodoco" / name).is_dir()
        or not (node_modules / "@banodoco" / name / "package.json").is_file()
    ]
    if missing:
        return RemotionRuntimeStatus(
            False,
            project_dir,
            "required Remotion package(s) missing: " + ", ".join(missing),
        )
    if not (project_dir / REMOTION_CLI_RELATIVE_PATH).is_file():
        return RemotionRuntimeStatus(False, project_dir,
                                     f"locked Remotion CLI is missing: {project_dir / REMOTION_CLI_RELATIVE_PATH}")
    schema_root_raw = os.environ.get(TIMELINE_SCHEMA_PYTHONPATH_ENV, "").strip()
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
    runtime_tools, tools_error = resolve_remotion_runtime_tools(project_dir)
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


__all__ = [
    "NODE_EXECUTABLE_ENV",
    "REMOTION_CLI_RELATIVE_PATH",
    "REMOTION_PROJECT_DIR_ENV",
    "RemotionRuntimeTools",
    "TIMELINE_SCHEMA_PYTHONPATH_ENV",
    "RemotionRuntimeStatus",
    "resolve_remotion_project_dir",
    "resolve_remotion_runtime_tools",
    "remotion_runtime_status",
    "timeline_requires_remotion",
]
