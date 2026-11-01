"""Execution helpers for Astrid executor definitions."""

from __future__ import annotations

import argparse
import importlib.abc
import importlib.util
import keyword
import uuid
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from astrid.core._shared.capability_common import (
    _has_value,
    _output_value,
    _stringify_value,
    _validate_required_inputs,
)
from astrid.core.contracts.binding import (
    BindingError,
    assert_provided_inputs_bound,
    expand_command,
)
from astrid.core._shared.result_manifest import HarvestError, harvest_staged_outputs
from astrid.core.contracts.capability_runner import CapabilityRunner
from astrid.core.contracts.exec_error import (
    ExecError,
    error_from_missing_binaries,
    error_from_returncode,
)
from astrid.core.contracts.errors import AstridError
from astrid.core.contracts.run_status import RunStatus
from astrid.core.contracts.scoped_config import SCOPE_REGISTRY, ScopeRequest
from astrid.core.env_vars import ASTRID_INTERNAL_INVOCATION
from astrid.core.foundation.hash import executor_definition_digest
from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.generation.vibecomfy_dependency import dependency_pythonpath
from astrid.core.media import require_runtime_materialized_file
from astrid.core.project.guidance import (
    format_project_required_guidance,
    selected_project,
)
from astrid.core.project.ownership import require_project_owned_artifact
from astrid.core.project.runtime import (
    _project_subprocess_env,
    reject_project_with_out,
)
from astrid.core.runtime.log_capture import (
    open_run_log_capture,
    run_subprocess_with_capture,
)
from astrid.core.subprocess_env import build_child_subprocess_env

from .registry import ExecutorRegistry, load_default_registry
from .schema import (
    ConditionSpec,
    ExecutorDefinition,
    ExecutorKind,
    ExecutorValidationError,
)


class ExecutorRunnerError(ExecutorValidationError):
    """Raised when a executor cannot be prepared or executed."""


@lru_cache(maxsize=None)
def _pipeline_module(runtime_module: str):
    """Import a pipeline driver module by its dotted path.

    The path is supplied by the orchestrator-tier caller via the executor
    manifest (``metadata.pipeline_module``); the executor never reaches up into
    ``astrid.core.execution.orchestrator`` to discover it.
    """
    if not isinstance(runtime_module, str) or not runtime_module:
        raise ExecutorRunnerError("pipeline executor manifest is missing metadata.pipeline_module")
    # Pipeline drivers are implementation modules shared by the canonical
    # runner and the lower-level orchestrator.  Their module files retain the
    # public-entrypoint guard, so importing one for SDK dispatch must carry the
    # same internal marker as the runner's subprocess command without leaking
    # that marker into the caller's environment.
    previous = os.environ.get(ASTRID_INTERNAL_INVOCATION)
    os.environ[ASTRID_INTERNAL_INVOCATION] = "1"
    try:
        return import_module(runtime_module)
    finally:
        if previous is None:
            os.environ.pop(ASTRID_INTERNAL_INVOCATION, None)
        else:
            os.environ[ASTRID_INTERNAL_INVOCATION] = previous


def _pipeline_module_for_executor(executor: ExecutorDefinition):
    """Resolve the pipeline driver module hosting this executor's steps.

    A pipeline-step executor declares ``metadata.command_builder`` as
    ``<pipeline_module>.build_pool_steps``; the driver module (which also owns
    ``STEP_ORDER``) is that path minus the trailing function name. Deriving it
    from ``command_builder`` keeps a single source of truth — every pipeline-step
    executor already declares it, so a new one needs no extra field — and keeps
    the executor decoupled from ``astrid.core.execution.orchestrator``.
    """
    command_builder = executor.metadata.get("command_builder")
    if not isinstance(command_builder, str) or "." not in command_builder:
        raise ExecutorRunnerError(
            f"built-in executor {executor.id!r} is missing metadata.command_builder"
        )
    return _pipeline_module(command_builder.rsplit(".", 1)[0])


def _pipeline_steps_by_name(executor: ExecutorDefinition) -> Mapping[str, Any]:
    pipeline = _pipeline_module_for_executor(executor)
    steps = {step.name: step for step in pipeline.build_pool_steps()}
    missing = [name for name in pipeline.STEP_ORDER if name not in steps]
    if missing:
        raise ValueError(f"build_pool_steps() is missing STEP_ORDER entries: {', '.join(missing)}")
    return MappingProxyType(steps)


@dataclass(frozen=True)
class ExecutorRunRequest:
    executor_id: str
    out: Path | str | None
    project: str | None = None
    inputs: Mapping[str, Any] = field(default_factory=dict)
    outputs: Mapping[str, Any] = field(default_factory=dict)
    brief: Path | str | None = None
    dry_run: bool = False
    check_binaries: bool = False
    python_exec: str | None = None
    verbose: bool = False
    argv: tuple[str, ...] = ()
    project_was_auto_resolved: bool = False
    invocation: str = "cli"
    projects_root: Path | str | None = None
    run_root: Path | str | None = None
    run_id: str | None = None
    project_run_metadata: Mapping[str, Any] = field(default_factory=dict)
    expected_executor_version: str | None = None


@dataclass(frozen=True)
class ExecutorRunResult:
    executor_id: str
    kind: ExecutorKind
    command: tuple[str, ...] = ()
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    returncode: int | None = None
    dry_run: bool = False
    skipped: bool = False
    skipped_reason: str = ""
    missing_binaries: tuple[str, ...] = ()
    error: ExecError | None = None
    # ── A1 identity fields (backward-compatible defaults) ─────────────────
    run_root: Path | str | None = None
    outputs: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    executor_version: str = ""  # derived from executor_definition_digest
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.error is None:
            derived = error_from_missing_binaries(self.missing_binaries) or error_from_returncode(
                self.returncode
            )
            if derived is not None:
                object.__setattr__(self, "error", derived)

    @property
    def ok(self) -> bool:
        return self.error is None


class ExecutorCapabilityRunner(CapabilityRunner[ExecutorRunRequest, ExecutorRunResult, ExecutorDefinition]):
    """Executor binding of the shared :class:`CapabilityRunner` skeleton."""

    def __init__(self, *, admission: Mapping[str, Any] | None = None) -> None:
        self._admission = admission

    def load_default_registry(self) -> ExecutorRegistry:
        return load_default_registry()

    def request_id(self, request: ExecutorRunRequest) -> str:
        return request.executor_id

    def validate_definition(
        self, request: ExecutorRunRequest, definition: ExecutorDefinition
    ) -> None:
        expected = request.expected_executor_version
        actual = executor_definition_digest(definition)
        if expected is not None and expected != actual:
            raise ExecutorRunnerError(
                f"executor definition changed after admission for {definition.id!r}; retry the invocation"
            )

    def build_command(
        self, request: ExecutorRunRequest, registry: ExecutorRegistry | None = None
    ) -> tuple[str, ...]:
        active_registry = registry or self.load_default_registry()
        executor = active_registry.get(request.executor_id)
        values = _request_values(request, executor)
        _validate_declared_input_choices(executor, values)
        _validate_required_inputs(
            executor.id, executor.inputs, values, noun="executor", error_cls=ExecutorRunnerError
        )
        condition_result = evaluate_conditions(executor, values)
        if condition_result.skipped:
            return ()
        if executor.command is not None:
            return _expand_external_command(executor, request, values)[0]
        if executor.kind == "built_in" and "pipeline_step" in executor.metadata:
            step = _step_for_executor(executor)
            args = build_pipeline_context(request, executor)
            return tuple(step.build_cmd(args))
        return _expand_external_command(executor, request, values)[0]

    def prepare_project(
        self, request: ExecutorRunRequest, definition: ExecutorDefinition
    ) -> tuple[object | None, ExecutorRunRequest]:
        return _prepare_project_request(request, definition)

    def resolve_project_request(
        self, request: ExecutorRunRequest, definition: ExecutorDefinition
    ) -> ExecutorRunRequest:
        return _resolve_project_request(request)

    def is_dry_run(self, request: ExecutorRunRequest, definition: ExecutorDefinition) -> bool:
        return bool(request.dry_run)

    def prepare_dry_run_request(
        self, request: ExecutorRunRequest, definition: ExecutorDefinition
    ) -> ExecutorRunRequest:
        return _prepare_dry_run_request(request)

    def run_inner(self, request: ExecutorRunRequest, definition: ExecutorDefinition) -> ExecutorRunResult:
        return _run_executor_inner(request, definition, _admission=self._admission)

    def finalize_project(
        self,
        context: object,
        request: ExecutorRunRequest,
        *,
        status: RunStatus,
        returncode: int | None,
        error: BaseException | str | None = None,
    ) -> None:
        _finalize_project_executor(
            context, request, status=status, returncode=returncode, error=error
        )

    def status_for_result(self, result: ExecutorRunResult) -> RunStatus:
        return _project_status_for_result(result)

    def result_returncode(self, result: ExecutorRunResult) -> int | None:
        return result.returncode


_EXECUTOR_RUNNER = ExecutorCapabilityRunner()


def run_executor(request: ExecutorRunRequest, registry: ExecutorRegistry | None = None,
                 *, _admission: Mapping[str, Any] | None = None) -> ExecutorRunResult:
    runner = ExecutorCapabilityRunner(admission=_admission) if _admission is not None else _EXECUTOR_RUNNER
    return runner.run(request, registry)


def _run_executor_inner(request: ExecutorRunRequest, executor: ExecutorDefinition,
                        *, _admission: Mapping[str, Any] | None = None) -> ExecutorRunResult:
    _validate_scoped_configs_at_dispatch(executor)
    invocation = executor.metadata.get("action_invocation")
    python_action = isinstance(invocation, Mapping) and invocation.get("kind") == "python"
    values = _action_input_values(executor, request) if python_action else _request_values(request, executor)
    if python_action:
        from astrid.sdk.actions import validate_action_inputs_definition
        validate_action_inputs_definition(executor, values)
    _validate_declared_input_choices(executor, values)
    if not python_action or "action_inputs_schema" not in executor.metadata:
        _validate_required_inputs(
            executor.id, executor.inputs, values, noun="executor", error_cls=ExecutorRunnerError
        )
    condition_result = evaluate_conditions(executor, values)
    if condition_result.skipped:
        return ExecutorRunResult(
            executor_id=executor.id,
            kind=executor.kind,
            payload={"executor_id": executor.id, "skipped": True, "skipped_reason": condition_result.reason},
            dry_run=request.dry_run,
            skipped=True,
            skipped_reason=condition_result.reason,
            run_id=request.run_id,
            run_root=request.run_root,
            executor_version=executor_definition_digest(executor),
        )

    missing_binaries = check_executor_binaries(executor) if request.check_binaries else ()
    if missing_binaries:
        return ExecutorRunResult(
            executor_id=executor.id,
            kind=executor.kind,
            payload={"executor_id": executor.id, "missing_binaries": list(missing_binaries)},
            dry_run=request.dry_run,
            missing_binaries=missing_binaries,
            run_id=request.run_id,
            run_root=request.run_root,
            executor_version=executor_definition_digest(executor),
        )

    if python_action:
        return _run_python_action(executor, request, values, _admission=_admission)
    if executor.kind == "built_in" and "pipeline_step" in executor.metadata:
        return _run_builtin_executor(executor, request)
    return _run_external_executor(executor, request, values)


def _action_input_values(executor: ExecutorDefinition, request: ExecutorRunRequest) -> dict[str, Any]:
    values = {port.name: port.default for port in executor.inputs if port.default is not None}
    schema = executor.metadata.get("action_inputs_schema")
    if isinstance(schema, Mapping):
        for name, declaration in schema.get("properties", {}).items():
            if isinstance(declaration, Mapping) and "default" in declaration:
                values[name] = declaration["default"]
    values.update(request.inputs)
    return values


class _PackSourceLoader(importlib.abc.Loader):
    """Compile admitted Python source directly; never consult pack bytecode."""

    def __init__(self, source: Path | None, directory: Path) -> None:
        self.source = source
        self.directory = directory

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        if self.source is not None:
            module.__file__ = str(self.source)
            exec(compile(self.source.read_bytes(), str(self.source), "exec"), module.__dict__)


class _PackSourceFinder(importlib.abc.MetaPathFinder):
    """Own only one opaque pack prefix, including package initializers."""

    def __init__(self, prefix: str, root: Path, path_names: Mapping[str, str]) -> None:
        self.prefix, self.root, self.path_names = prefix, root, path_names

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.prefix and not fullname.startswith(self.prefix + "."):
            return None
        parts = fullname.split(".")[1:]
        if any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
            raise ExecutorRunnerError("invalid private action module name")
        base = self.root.joinpath(*(self.path_names.get(part, part) for part in parts))
        directory = base.resolve()
        source = None
        package = directory.is_dir()
        if package:
            initializer = base / "__init__.py"
            if initializer.exists():
                source = initializer.resolve()
        else:
            source = Path(str(base) + ".py").resolve()
            if not source.is_file():
                # Do not let the default finder pick up cached/native modules.
                raise ModuleNotFoundError(f"no admitted Python source for {fullname!r}")
        if not directory.is_relative_to(self.root) or (source is not None and not source.is_relative_to(self.root)):
            raise ExecutorRunnerError("private action import escapes admitted pack root")
        loader = _PackSourceLoader(source, directory)
        spec = importlib.util.spec_from_loader(fullname, loader, is_package=package)
        if package:
            spec.submodule_search_locations = [str(directory)]
        return spec


def _run_python_action(executor: ExecutorDefinition, request: ExecutorRunRequest,
                       values: Mapping[str, Any], *, _admission: Mapping[str, Any] | None) -> ExecutorRunResult:
    from astrid.core.execution.generic_host import _verify_action_admission, _verify_admitted_source
    from astrid.sdk.actions import validate_action_output_definition

    invocation = executor.metadata["action_invocation"]
    root = Path(str(executor.metadata["pack_root"])).expanduser().resolve()
    raw_path, function_name = invocation.get("path"), invocation.get("function")
    relative = Path(raw_path) if isinstance(raw_path, str) else Path()
    if (not isinstance(raw_path, str) or relative.is_absolute() or "\\" in raw_path
            or "\x00" in raw_path or any(part in ("", ".", "..") for part in raw_path.split("/"))
            or not relative.parts or relative.parts[0] != "actions" or relative.suffix != ".py"
            or not isinstance(function_name, str) or not function_name.isidentifier() or keyword.iskeyword(function_name)):
        raise ExecutorRunnerError("invalid Python action path/function")
    source = (root / relative).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ExecutorRunnerError("Python action entrypoint is missing or escapes admitted pack root")
    if request.dry_run:
        return ExecutorRunResult(executor_id=executor.id, kind=executor.kind, dry_run=True,
                                 payload={"executor_id": executor.id},
                                 executor_version=executor_definition_digest(executor),
                                 run_id=request.run_id, run_root=request.run_root)
    if os.environ.get(ASTRID_INTERNAL_INVOCATION) != "1" or _admission is None:
        raise ExecutorRunnerError("Python actions require the admitted internal host worker")
    _verify_action_admission(executor.to_dict(), _admission)
    _verify_admitted_source(_admission)
    prefix = "_astrid_pack_" + uuid.uuid4().hex
    path_names = {}
    module_parts = []
    for part in (*relative.parts[:-1], relative.stem):
        name = part if part.isidentifier() and not keyword.iskeyword(part) else "_path_" + uuid.uuid4().hex
        if name != part:
            path_names[name] = part
        module_parts.append(name)
    finder = _PackSourceFinder(prefix, root, path_names)
    sys.meta_path.insert(0, finder)
    try:
        module = import_module(prefix + "." + ".".join(module_parts))
        function = getattr(module, function_name, None)
        if not callable(function):
            raise ExecutorRunnerError(f"Python action function {function_name!r} is missing or not callable")
        action_result = function(**dict(values))
        validate_action_output_definition(executor, action_result)
    finally:
        sys.meta_path.remove(finder)
        for name in tuple(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                del sys.modules[name]
    return ExecutorRunResult(executor_id=executor.id, kind=executor.kind,
                             payload={"action_result": action_result}, returncode=0,
                             outputs=_resolve_declared_outputs(executor, request),
                             executor_version=executor_definition_digest(executor),
                             run_id=request.run_id, run_root=request.run_root)


def _resolve_declared_outputs(
    executor: ExecutorDefinition,
    request: ExecutorRunRequest,
) -> list[dict[str, Any]]:
    """Harvest concrete files from the assigned spool.

    A present ``{out}/manifest.json`` is exclusive. Only legacy, non-media
    definitions without the receipt flag may fall back to concrete files at
    their declared ``path_template`` locations.
    """
    effective_out = request.out if request.out not in (None, "") else request.run_root
    if effective_out is None or effective_out == "":
        return []
    try:
        temp_request = replace(request, out=effective_out)
        values = _request_values(temp_request)
        placeholders = _placeholder_values(executor, temp_request, values)
    except ExecutorRunnerError:
        placeholders = {"out": str(effective_out)}
        values = {}
    try:
        return harvest_staged_outputs(
            Path(str(effective_out)).expanduser().resolve(),
            definition=executor,
            declared_outputs=executor.outputs,
            values={**values, **placeholders, "out": str(effective_out)},
            require=False,
        )
    except HarvestError as exc:
        raise ExecutorRunnerError(str(exc)) from exc


def resolve_declared_output_paths(
    executor: ExecutorDefinition,
    request: ExecutorRunRequest,
) -> dict[str, str]:
    """Resolve declared-output paths *without* requiring files to exist on disk.

    This is the non-existence-filtering companion to
    :func:`_resolve_declared_outputs`.  It uses the same placeholder-resolution
    pipeline so callers can learn which paths *would* be produced by a run
    (e.g. for CAS cache-hit checks) before the run has executed and before
    those files have been written to disk.

    Returns an empty mapping when there are no declared outputs or when no
    base output directory (``out`` / ``run_root``) is available.
    """
    if not executor.outputs:
        return {}
    effective_out = request.out if request.out not in (None, "") else request.run_root
    if effective_out is None or effective_out == "":
        return {}
    try:
        temp_request = replace(request, out=effective_out)
        values = _request_values(temp_request)
        placeholders = _placeholder_values(executor, temp_request, values)
    except ExecutorRunnerError:
        return {}
    resolved: dict[str, str] = {}
    for output in executor.outputs:
        output_path_str = placeholders.get(output.name)
        if output_path_str:
            resolved[output.name] = output_path_str
    return resolved


@dataclass(frozen=True)
class ConditionResult:
    skipped: bool = False
    reason: str = ""


def evaluate_conditions(executor: ExecutorDefinition, values: Mapping[str, Any]) -> ConditionResult:
    for condition in executor.conditions:
        result = _evaluate_condition(condition, values)
        if result.skipped:
            return result
    return ConditionResult()


def check_executor_binaries(executor: ExecutorDefinition) -> tuple[str, ...]:
    return tuple(binary for binary in executor.isolation.binaries if shutil.which(binary) is None)


def build_pipeline_context(request: ExecutorRunRequest, executor: ExecutorDefinition | None = None) -> argparse.Namespace:
    from astrid.core.theme import load_runtime_theme

    values = _request_values(request, executor)
    effective_out = request.out if request.out not in (None, "") else request.run_root
    if effective_out in (None, ""):
        raise ExecutorRunnerError(
            f"executor {request.executor_id!r} requires an output or staging path"
        )
    out = Path(effective_out).expanduser().resolve()
    brief = _optional_path(values.get("brief") or request.brief)
    if brief is None:
        brief = (out / "brief.txt").resolve()
    audio_value = values.get("audio")
    video_value = values.get("video")
    video = _optional_asset_path(video_value)
    audio = _optional_asset_path(audio_value if audio_value is not None else video_value)
    env_file = _optional_path(values.get("env_file"))
    theme_raw = values.get("theme")
    theme_explicit = theme_raw is not None
    if theme_explicit:
        candidate = Path(theme_raw).expanduser()
        load_runtime_theme(candidate)
        theme = candidate.resolve()
    else:
        # No theme folder or ambient scope participates in dispatch.  The
        # renderer's intentional built-in style is selected downstream.
        theme = None
    brief_slug = str(values.get("brief_slug") or _default_brief_slug(brief, out))
    brief_out = (out / "briefs" / brief_slug).resolve()
    skip = _as_string_list(values.get("skip"))
    asset_values = _as_string_list(values.get("asset") or values.get("assets"))
    args = argparse.Namespace(
        audio=audio,
        video=video,
        out=out,
        brief=brief,
        brief_out=brief_out,
        brief_copy=brief_out / "brief.txt",
        skip=skip,
        asset=asset_values,
        asset_pairs=_parse_asset_pairs(asset_values),
        primary_asset=values.get("primary_asset"),
        theme=theme,
        theme_explicit=theme_explicit,
        source_slug=str(values.get("source_slug") or out.name),
        brief_slug=brief_slug,
        env_file=env_file,
        extra_args=_normalize_extra_args(values.get("extra_args")),
        target_duration=_optional_float(values.get("target_duration")),
        python_exec=str(values.get("python_exec") or request.python_exec or sys.executable),
        render=bool(values.get("render", False)),
        verbose=bool(values.get("verbose", request.verbose)),
        no_prefetch=bool(values.get("no_prefetch", False)),
        cache_dir=_optional_path(values.get("cache_dir")),
        drift=str(values.get("drift") or "strict"),
        from_step=values.get("from_step"),
        max_editor_passes=int(values.get("max_editor_passes", 2)),
        editor_iteration=int(values.get("editor_iteration", 1)),
    )
    if executor is not None:
        args.executor_id = executor.id
    return args


def build_executor_command(request: ExecutorRunRequest, registry: ExecutorRegistry | None = None) -> tuple[str, ...]:
    return _EXECUTOR_RUNNER.build_command(request, registry)


def _run_builtin_executor(executor: ExecutorDefinition, request: ExecutorRunRequest) -> ExecutorRunResult:
    if executor.command is not None:
        return _run_explicit_command_executor(executor, request, _request_values(request, executor))
    pipeline = _pipeline_module_for_executor(executor)
    step = _step_for_executor(executor)
    args = build_pipeline_context(request, executor)
    command = tuple(step.build_cmd(args))
    if request.dry_run:
        return ExecutorRunResult(
            executor_id=executor.id,
            kind=executor.kind,
            command=command,
            payload={"executor_id": executor.id, "missing_binaries": [], "returncode": None, "skipped": False, "skipped_reason": ""},
            dry_run=True,
            run_id=request.run_id,
            run_root=request.run_root,
            executor_version=executor_definition_digest(executor),
        )
    if args.brief.exists():
        pipeline.prepare_brief_artifacts(args)
    returncode = pipeline.run_step(step, list(command), args)
    return ExecutorRunResult(
        executor_id=executor.id,
        kind=executor.kind,
        command=command,
        payload={"executor_id": executor.id, "missing_binaries": [], "returncode": returncode, "skipped": False, "skipped_reason": ""},
        returncode=returncode,
        run_id=request.run_id,
        run_root=request.run_root,
        executor_version=executor_definition_digest(executor),
        outputs=_resolve_declared_outputs(executor, request) if returncode == 0 else [],
    )


def _run_explicit_command_executor(
    executor: ExecutorDefinition,
    request: ExecutorRunRequest,
    values: Mapping[str, Any],
) -> ExecutorRunResult:
    command, cwd, env = _expand_external_command(executor, request, values)
    if request.dry_run:
        return ExecutorRunResult(
            executor_id=executor.id,
            kind=executor.kind,
            command=command,
            cwd=cwd,
            env=env,
            payload={"executor_id": executor.id, "missing_binaries": [], "returncode": None, "skipped": False, "skipped_reason": ""},
            dry_run=True,
            run_id=request.run_id,
            run_root=request.run_root,
            executor_version=executor_definition_digest(executor),
        )
    effective_env = _command_subprocess_env(executor, request, env)
    run_root = request.run_root
    if run_root is not None and not request.project_was_auto_resolved:
        with open_run_log_capture(run_root) as logs:
            returncode = run_subprocess_with_capture(
                list(command),
                cwd=cwd,
                env=effective_env,
                stdout_log=logs.stdout,
                stderr_log=logs.stderr,
            )
    else:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=effective_env,
            check=False,
        )
        returncode = completed.returncode
    return ExecutorRunResult(
        executor_id=executor.id,
        kind=executor.kind,
        command=command,
        cwd=cwd,
        env=env,
        payload={
            "executor_id": executor.id,
            "missing_binaries": [],
            "returncode": returncode,
            "skipped": False,
            "skipped_reason": "",
        },
        returncode=returncode,
        run_id=request.run_id,
        run_root=request.run_root,
        executor_version=executor_definition_digest(executor),
        outputs=_resolve_declared_outputs(executor, request) if returncode == 0 else [],
    )


def _run_external_executor(executor: ExecutorDefinition, request: ExecutorRunRequest, values: Mapping[str, Any]) -> ExecutorRunResult:
    return _run_explicit_command_executor(executor, request, values)


def _expand_external_command(
    executor: ExecutorDefinition,
    request: ExecutorRunRequest,
    values: Mapping[str, Any],
) -> tuple[tuple[str, ...], str | None, dict[str, str]]:
    if executor.command is None:
        raise ExecutorRunnerError(f"executor {executor.id!r} has no command")
    placeholders = _placeholder_values(executor, request, values)
    binding_values: dict[str, Any] = dict(placeholders)
    for port in executor.inputs:
        if port.name not in values and port.default is not None:
            binding_values[port.name] = port.default
    binding_values.update(values)
    try:
        result = expand_command(
            executor.command,
            executor.inputs,
            binding_values,
            executor.metadata,
        )
        assert_provided_inputs_bound(
            result,
            executor.inputs,
            binding_values,
            executor.metadata,
        )
    except BindingError as exc:
        raise ExecutorRunnerError(f"executor {executor.id!r}: {exc}") from exc
    return result.argv, result.cwd, result.env


def _prepare_project_request(
    request: ExecutorRunRequest,
    executor: ExecutorDefinition,
    ) -> tuple[object | None, ExecutorRunRequest]:
    # Single-ledger cut: project runs are kernel-owned (RunRepository fan-out).
    # The runner retains the output directory as staging only; no run.json is
    # written here. The kernel admission path (sdk.invoke / CapabilityTaskHandler)
    # owns the authoritative run/task ledger. Preserve out as staging.
    if not request.project:
        return None, request
    _validate_project_owned_inputs(request, executor)
    if request.out in (None, ""):
        raise ExecutorRunnerError(
            "project-scoped executor execution requires kernel admission to "
            "supply a staging output directory"
        )
    if not request.project_was_auto_resolved:
        reject_project_with_out(request.project, request.out)
    # A project-scoped lower-level call may use caller output or a kernel-owned
    # staging root, but it must never mint a project run directory itself. The
    # SDK/kernel admission path supplies ``run_root`` before reaching here;
    # without either path, fail closed rather than creating an orphaned second
    # ledger surface under ``project/runs``.
    if request.out in (None, "") and request.run_root in (None, ""):
        raise ExecutorRunnerError(
            f"executor {request.executor_id!r} requires an output or staging path"
        )
    # Keep out unchanged as staging/output; run dir is output/staging only.
    return None, request


def _project_run_metadata(
    request: ExecutorRunRequest,
    executor: ExecutorDefinition,
) -> dict[str, Any]:
    declared = executor.metadata.get("run_metadata")
    if declared is not None and not isinstance(declared, Mapping):
        raise ExecutorRunnerError(
            f"executor {executor.id!r} metadata.run_metadata must be an object"
        )
    metadata = dict(declared or {})
    metadata.update(
        {
            "dry_run": bool(request.dry_run),
            "executor_version": executor_definition_digest(executor),
            "project_resolution": (
                "attached" if request.project_was_auto_resolved else "explicit"
            ),
        }
    )
    return metadata


def _validate_project_owned_inputs(
    request: ExecutorRunRequest,
    executor: ExecutorDefinition,
) -> None:
    """Require declared inputs to be attempt-local runtime artifacts.

    Live generic-host requests have no project-tree authority.  A legacy
    ``projects_root`` is accepted only for explicit offline callers that have
    opted into the old ownership check; normal runtime requests must resolve
    every path beneath their assigned output/run attempt.
    """

    if not request.project:
        return
    for port in getattr(executor, "inputs", ()):
        artifact_type = port.artifact_type
        if not isinstance(artifact_type, str):
            continue
        normalized = artifact_type.strip().lower().replace("-", "_")
        if not (
            normalized == "timeline"
            or normalized.startswith("timeline/")
            or normalized == "experiment"
            or normalized.startswith("experiment/")
            or normalized in {"project_runs", "experiment_runs"}
        ):
            continue
        value = request.inputs.get(port.name)
        if not _has_value(value):
            continue
        attempt_roots = tuple(
            Path(str(root)).expanduser().resolve()
            for root in (request.out, request.run_root)
            if root not in (None, "")
        )
        for item in _iter_input_values(value):
            raw = _stringify_value(item)
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute() and attempt_roots:
                candidate = (attempt_roots[0] / candidate).resolve()
            else:
                candidate = candidate.resolve()
            if any(candidate == root or root in candidate.parents for root in attempt_roots):
                continue
            if request.projects_root is None:
                raise ExecutorRunnerError(
                    f"{normalized} input {raw!r} must be materialized beneath the assigned attempt"
                )
            require_project_owned_artifact(
                request.project,
                normalized,
                raw,
                root=request.projects_root,
            )




def _project_argv(request: ExecutorRunRequest) -> list[str]:
    argv = ["executors", "run", request.executor_id]
    if request.project:
        argv.extend(["--project", request.project])
    if request.brief:
        argv.extend(["--brief", str(request.brief)])
    for key, value in request.inputs.items():
        for item in _iter_input_values(value):
            argv.extend(["--input", f"{key}={_stringify_value(item)}"])
    if request.dry_run:
        argv.append("--dry-run")
    if request.check_binaries:
        argv.append("--check-binaries")
    if request.python_exec:
        argv.extend(["--python-exec", request.python_exec])
    if request.verbose:
        argv.append("--verbose")
    return argv


def _project_status_for_result(result: ExecutorRunResult) -> RunStatus:
    if result.skipped or result.dry_run:
        return RunStatus.SKIPPED
    if not result.ok:
        return RunStatus.FAILED
    return RunStatus.COMPLETED


def _finalize_project_executor(
    context: object,
    request: ExecutorRunRequest,
    *,
    status: RunStatus,
    returncode: int | None,
    error: BaseException | str | None = None,
) -> None:
    # Single-ledger cut: no authoritative run.json finalize here. The kernel
    # owns terminal status; this remains as a derived-projection hook (no-op
    # when called with a None context, which is the normal path). Run
    # directories are storage only.
    return


def _resolve_project_request(request: ExecutorRunRequest) -> ExecutorRunRequest:
    project, source = selected_project(request.project)
    if source == "explicit":
        return request
    if project is not None:
        return replace(
            request,
            project=project,
            project_was_auto_resolved=True,
        )
    raise ExecutorRunnerError(format_project_required_guidance(operation="executor run"))

def _prepare_dry_run_request(request: ExecutorRunRequest) -> ExecutorRunRequest:
    if request.out not in (None, ""):
        return request
    placeholder = (Path.cwd() / ".astrid-dry-run" / request.executor_id.replace(".", "-")).resolve()
    return replace(request, out=placeholder)


def _validate_scoped_configs_at_dispatch(executor: ExecutorDefinition) -> None:
    """Validate declared scoped_configs keys against SCOPE_REGISTRY at dispatch time.

    Called by _run_executor_inner before execution. Raises ExecutorValidationError for
    any key that has no registered resolver — shape-only validation at parse time means
    this is the first point where tier-3 registry state is consulted.
    """
    if not executor.scoped_configs:
        return
    import astrid.core.util.credentials_scope  # noqa: F401 — side-effect: registers 'credentials.*'
    for key in executor.scoped_configs:
        if not SCOPE_REGISTRY.is_registered(key):
            raise ExecutorValidationError(
                f"executor {executor.id!r} declares unknown scoped_config key {key!r}"
            )


def _validate_declared_input_choices(
    executor: ExecutorDefinition, values: Mapping[str, Any]
) -> None:
    """Validate manifest-declared input enums before command construction.

    Most constrained inputs are enforced by a capability's own argparse
    parser.  Dry-run intentionally does not start that subprocess, however,
    so small dispatcher capabilities can declare their enum in metadata and
    receive the same typed validation before a command is admitted or built.
    """
    choices = executor.metadata.get("input_choices")
    if isinstance(choices, Mapping):
        for input_name, raw_options in choices.items():
            if not isinstance(input_name, str) or not isinstance(raw_options, (list, tuple)):
                continue
            value = values.get(input_name)
            if value is None:
                continue
            options = tuple(str(option) for option in raw_options)
            if str(value) in options:
                continue
            rendered = ", ".join(options)
            raise ExecutorRunnerError(
                f"invalid {input_name} {value!r} for executor {executor.id!r}; "
                f"valid options: {rendered}; "
                f"recovery: retry with --{input_name.replace('_', '-')} "
                f"<one of: {rendered}>"
            )

    requirements = executor.metadata.get("input_requirements_by_choice")
    if not isinstance(requirements, Mapping):
        return
    for selector, raw_requirements in requirements.items():
        selected = values.get(str(selector))
        if selected is None or not isinstance(raw_requirements, Mapping):
            continue
        required_inputs = raw_requirements.get(str(selected))
        if not isinstance(required_inputs, (list, tuple)):
            continue
        missing = [
            str(name) for name in required_inputs
            if not _has_value(values.get(str(name)))
        ]
        if missing:
            names = ", ".join(missing)
            raise ExecutorRunnerError(
                f"missing required input(s) for {selector} {selected!r}: {names}; "
                f"recovery: provide --{missing[0].replace('_', '-')} and retry"
            )


def _emit_scoped_config_env(
    executor: ExecutorDefinition, request: ExecutorRunRequest
) -> dict[str, str]:
    """Resolve declared scoped_configs and return their subprocess env contributions.

    Style/theme is an explicit task input, never a scoped environment value.
    Credentials remain the only scoped configuration emitted here.
    """
    if not executor.scoped_configs:
        return {}
    import astrid.core.util.credentials_scope  # noqa: F401
    scope_request = ScopeRequest(
        project_slug=request.project,
        env=dict(os.environ),
        explicit=None,
    )
    env: dict[str, str] = {}
    for key in executor.scoped_configs:
        if key.startswith("credentials."):
            from astrid.core.util.credentials_scope import _PROVIDER_ENV, CredentialsScope
            try:
                result = SCOPE_REGISTRY.resolve(key, scope_request)
            except AstridError:
                # A declared provider may not be used by the selected route.
                # Leave absent keys out; the provider reports the recovery
                # command if a cloud call actually needs the credential.
                continue
            if isinstance(result, CredentialsScope):
                provider_env = _PROVIDER_ENV.get(result.provider)
                if provider_env:
                    env[provider_env] = result.value  # scoped-config emit
    return env


def _command_subprocess_env(
    executor: ExecutorDefinition,
    request: ExecutorRunRequest,
    command_env: Mapping[str, str],
) -> dict[str, str]:
    external_pack_env = _external_pack_pythonpath_env(executor, command_env)
    project_env = _project_subprocess_env(request)
    scoped_env = _emit_scoped_config_env(executor, request)
    declared_secret_env = tuple(dict.fromkeys(
        str(name) for name in (
            *(executor.isolation.secrets_required or ()),
            *(executor.metadata.get("secrets_required") or ()),
            *(executor.metadata.get("required_env") or ()),
            *(executor.metadata.get("env") or ()),
        )
    ))
    explicit_env = {
        **command_env,
        **external_pack_env,
        **project_env,
        **scoped_env,
        "ASTRID_INTERNAL_INVOCATION": "1",
    }
    # The worker environment has already selected and validated the exact
    # VibeComfy source. Preserve only those approved dependency roots through
    # this final command-child boundary; ambient checkout paths stay filtered.
    try:
        dependency_roots = dependency_pythonpath()
    except ValueError as exc:
        raise ExecutorRunnerError(str(exc)) from exc
    if dependency_roots:
        existing_pythonpath = str(explicit_env.get("PYTHONPATH") or "")
        roots = tuple(
            value
            for value in (*existing_pythonpath.split(os.pathsep), *dependency_roots)
            if value
        )
        explicit_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(roots))
    # Scoped credential resolution is the only allowed source for secret
    # values.  Pass them through the dedicated in-memory secret channel rather
    # than the ordinary explicit environment map.
    secret_values = {
        key: value for key, value in scoped_env.items() if key in declared_secret_env
    }
    for key in secret_values:
        explicit_env.pop(key, None)
    return build_child_subprocess_env(
        # Project identity attached to the admitted request is authoritative;
        # no project-tree locator is exported to the child.
        parent={**os.environ, **project_env},
        explicit_env=explicit_env,
        passthrough=executor.isolation.env_passthrough,
        declared_passthrough=executor.isolation.env_passthrough,
        secret_values=secret_values,
        declared_secrets=declared_secret_env,
    )


def _external_pack_pythonpath_env(
    executor: ExecutorDefinition,
    command_env: Mapping[str, str],
) -> dict[str, str]:
    if executor.command is None:
        return {}
    argv = tuple(executor.command.argv)
    if len(argv) < 3 or argv[1] != "-m":
        return {}
    pack_id = str(executor.metadata.get("source_pack") or "")
    module = argv[2]
    if not pack_id or not module.startswith(f"{pack_id}."):
        return {}
    pack_root_raw = executor.metadata.get("pack_root")
    if not isinstance(pack_root_raw, str) or not pack_root_raw:
        return {}
    pack_root = Path(pack_root_raw).expanduser().resolve()
    builtin_root = (REPO_ROOT / "astrid" / "packs" / pack_id).resolve()
    if pack_root == builtin_root:
        return {}
    pack_parent = str(pack_root.parent)
    existing = command_env.get("PYTHONPATH") or os.environ.get("PYTHONPATH")
    return {"PYTHONPATH": pack_parent if not existing else os.pathsep.join((pack_parent, existing))}


def _placeholder_values(executor: ExecutorDefinition, request: ExecutorRunRequest, values: Mapping[str, Any]) -> dict[str, str]:
    effective_out = request.out if request.out not in (None, "") else request.run_root
    if effective_out in (None, ""):
        raise ExecutorRunnerError(
            f"executor {executor.id!r} requires an output or staging path"
        )
    out = Path(effective_out).expanduser().resolve()
    placeholders: dict[str, str] = {
        "out": str(out),
    }
    python_exec = _resolve_python_exec(executor, request, values)
    if python_exec is not None:
        placeholders["python_exec"] = python_exec
    brief = values.get("brief") or request.brief
    if brief is not None:
        brief_path = Path(str(brief)).expanduser().resolve()
        placeholders["brief"] = str(brief_path)
        brief_slug = str(values.get("brief_slug") or _default_brief_slug(brief_path, out))
        brief_out = out / "briefs" / brief_slug
        placeholders["brief_slug"] = brief_slug
        placeholders["brief_out"] = str(brief_out)
        placeholders["brief_copy"] = str(brief_out / "brief.txt")
    for port in executor.inputs:
        if port.default is not None and port.name not in values:
            placeholders[port.name] = _stringify_value(port.default)
    for key, value in values.items():
        if value is None:
            continue
        placeholders[key] = _stringify_value(value)
    for output in executor.outputs:
        output_path = _output_value(output, request, placeholders, error_cls=ExecutorRunnerError)
        placeholders[output.name] = output_path
        if output.placeholder:
            placeholders[output.placeholder] = output_path
    return placeholders


def _resolve_python_exec(executor: ExecutorDefinition, request: ExecutorRunRequest, values: Mapping[str, Any]) -> str | None:
    input_override = values.get("python_exec")
    if _has_value(input_override):
        return str(input_override)
    if _has_value(request.python_exec):
        return str(request.python_exec)
    if not _executor_uses_placeholder(executor, "python_exec"):
        return None
    return sys.executable


def _executor_uses_placeholder(executor: ExecutorDefinition, placeholder: str) -> bool:
    if executor.command is None:
        return False
    needle = f"{{{placeholder}}}"
    if any(needle in part for part in executor.command.argv):
        return True
    if executor.command.cwd and needle in executor.command.cwd:
        return True
    return any(needle in value for value in executor.command.env.values())


def _evaluate_condition(condition: ConditionSpec, values: Mapping[str, Any]) -> ConditionResult:
    if condition.kind == "always":
        return ConditionResult()
    if condition.kind == "requires_input":
        if not condition.input or not _has_value(values.get(condition.input)):
            raise ExecutorRunnerError(f"condition requires input {condition.input!r}")
        return ConditionResult()
    if condition.kind == "requires_file":
        candidate = values.get(condition.input) if condition.input else condition.path
        if not _has_value(candidate):
            raise ExecutorRunnerError("condition requires a file path")
        path = Path(str(candidate)).expanduser()
        if not path.is_file():
            raise ExecutorRunnerError(f"condition requires file: {path}")
        return ConditionResult()
    if condition.kind == "skip_if_input" and condition.input and _has_value(values.get(condition.input)):
        return ConditionResult(skipped=True, reason=f"input {condition.input!r} is set")
    raise ExecutorRunnerError(f"unsupported condition kind {condition.kind!r}")


def _step_for_executor(executor: ExecutorDefinition) -> Any:
    step_name = executor.metadata.get("pipeline_step")
    if not isinstance(step_name, str):
        raise ExecutorRunnerError(f"built-in executor {executor.id!r} is missing metadata.pipeline_step")
    steps = _pipeline_steps_by_name(executor)
    if step_name not in steps:
        raise ExecutorRunnerError(f"built-in executor {executor.id!r} references unknown pipeline step {step_name!r}")
    return steps[step_name]


def _request_values(request: ExecutorRunRequest, executor: ExecutorDefinition | None = None) -> dict[str, Any]:
    values = dict(request.inputs)
    if request.brief is not None and "brief" not in values:
        values["brief"] = request.brief
    if request.python_exec is not None and "python_exec" not in values:
        values["python_exec"] = request.python_exec
    # A managed executor may expose its owning project as an explicit input
    # (for example timeline visualization's ``project_slug``) while the
    # public SDK carries the same identity in ``project=``.  Derive the
    # declared field before command expansion so in-process and subprocess
    # runners receive identical argv; callers can still override it when the
    # manifest deliberately allows a different standalone value.
    if (
        executor is not None
        and request.project
        and "project_slug" in {port.name for port in executor.inputs}
        and "project_slug" not in values
    ):
        values["project_slug"] = request.project
    values.setdefault("verbose", request.verbose)
    return values


def _optional_path(value: Any) -> Path | None:
    if value is None or value == "":
        return None
    return Path(str(value)).expanduser().resolve()


def _optional_asset_path(value: Any) -> Path | None:
    if value is None or value == "":
        return None
    try:
        return require_runtime_materialized_file(value)
    except ValueError as exc:
        raise ExecutorRunnerError(str(exc)) from exc


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _as_string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _parse_asset_pairs(values: list[str]) -> list[tuple[str, Path]]:
    pairs: list[tuple[str, Path]] = []
    for raw in values:
        if "=" not in raw:
            raise ExecutorRunnerError(f"invalid asset value {raw!r}; expected KEY=PATH")
        key, path_text = raw.split("=", 1)
        key = key.strip()
        path_text = path_text.strip()
        if not key or not path_text:
            raise ExecutorRunnerError(f"invalid asset value {raw!r}; expected KEY=PATH")
        try:
            pairs.append((key, require_runtime_materialized_file(path_text, label=f"asset {key!r}")))
        except ValueError as exc:
            raise ExecutorRunnerError(str(exc)) from exc
    return pairs


def _normalize_extra_args(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ExecutorRunnerError("extra_args must be an object keyed by step name")
    return {str(key): _as_string_list(raw_values) for key, raw_values in value.items()}


def _default_brief_slug(brief: Path, out: Path) -> str:
    generic_brief_names = {"brief", "plan", "prompt"}
    return out.name if brief.stem.lower() in generic_brief_names else brief.stem


def _iter_input_values(value: Any) -> tuple[Any, ...]:
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, tuple):
        return value
    return (value,)


__all__ = [
    "ConditionResult",
    "ExecutorCapabilityRunner",
    "ExecutorRunRequest",
    "ExecutorRunResult",
    "ExecutorRunnerError",
    "build_pipeline_context",
    "build_executor_command",
    "check_executor_binaries",
    "evaluate_conditions",
    "run_executor",
]
