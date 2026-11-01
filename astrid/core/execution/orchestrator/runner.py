"""Execution helpers for Astrid orchestrator definitions."""

from __future__ import annotations

import os
import subprocess
import sys
import pickle
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from astrid.core._shared.capability_common import (
    _PLACEHOLDER_RE,
    _expand_placeholders,
    _has_cli_option,
    _output_value,
    _stringify_value,
    _validate_required_inputs,
)
from astrid.core.contracts.capability_runner import CapabilityRunner
from astrid.core.contracts.binding import (omit_absent_optional_flag_values, expand_command,
    assert_provided_inputs_bound, BindingError)
from astrid.core.contracts.run_status import RunStatus
from astrid.core.project.guidance import (
    format_project_required_guidance,
    selected_project,
)
from astrid.core.project.runtime import (
    ProjectRuntimeError,
    _project_subprocess_env,
    reject_project_with_out,
)
from astrid.core.runtime._normalize import normalize_python_runtime_result
from astrid.core.runtime.log_capture import (
    open_run_log_capture,
    run_subprocess_with_capture,
)
from astrid.core.subprocess_env import build_child_subprocess_env

from .registry import OrchestratorRegistry, load_default_registry
from .schema import (
    OrchestratorDefinition,
    OrchestratorKind,
    OrchestratorValidationError,
    RuntimeKind,
)


class OrchestratorRunnerError(OrchestratorValidationError):
    """Raised when a orchestrator cannot be prepared or executed."""


@dataclass(frozen=True)
class OrchestratorRunRequest:
    orchestrator_id: str
    out: Path | str | None = None
    project: str | None = None
    inputs: Mapping[str, Any] = field(default_factory=dict)
    outputs: Mapping[str, Any] = field(default_factory=dict)
    brief: Path | str | None = None
    dry_run: bool = False
    python_exec: str | None = None
    verbose: bool = False
    project_was_auto_resolved: bool = False
    invocation: str = "cli"
    projects_root: Path | str | None = None
    run_root: Path | str | None = None
    run_id: str | None = None
    project_run_metadata: Mapping[str, Any] = field(default_factory=dict)
    orchestrator_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrchestratorRunError:
    message: str
    kind: str = "runtime"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "message": self.message}


@dataclass(frozen=True)
class OrchestratorPlanStep:
    id: str
    kind: str = "command"
    command: tuple[str, ...] = ()
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "command": list(self.command),
        }
        if self.description:
            payload["description"] = self.description
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class OrchestratorPlan:
    steps: tuple[OrchestratorPlanStep, ...] = ()
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"steps": [step.to_dict() for step in self.steps]}
        if self.summary:
            payload["summary"] = self.summary
        return payload


@dataclass(frozen=True)
class OrchestratorRunResult:
    orchestrator_id: str
    kind: OrchestratorKind
    runtime_kind: RuntimeKind
    command: tuple[str, ...] = ()
    planned_commands: tuple[tuple[str, ...], ...] = ()
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    returncode: int | None = None
    dry_run: bool = False
    outputs: Mapping[str, Any] = field(default_factory=dict)
    errors: tuple[OrchestratorRunError, ...] = ()
    plan: OrchestratorPlan | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and (self.returncode is None or self.returncode == 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "orchestrator_id": self.orchestrator_id,
            "kind": self.kind,
            "runtime_kind": self.runtime_kind,
            "command": list(self.command),
            "planned_commands": [list(command) for command in self.planned_commands],
            "cwd": self.cwd,
            "env": dict(self.env),
            "returncode": self.returncode,
            "dry_run": self.dry_run,
            "outputs": dict(self.outputs),
            "errors": [error.to_dict() for error in self.errors],
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "ok": self.ok,
        }


class OrchestratorCapabilityRunner(CapabilityRunner[OrchestratorRunRequest, OrchestratorRunResult, OrchestratorDefinition]):
    """Orchestrator binding of the shared :class:`CapabilityRunner` skeleton."""

    def load_default_registry(self) -> OrchestratorRegistry:
        return load_default_registry()

    def request_id(self, request: OrchestratorRunRequest) -> str:
        return request.orchestrator_id

    def build_command(
        self, request: OrchestratorRunRequest, registry: object | None = None
    ) -> tuple[str, ...]:
        active_registry = registry if isinstance(registry, OrchestratorRegistry) else self.load_default_registry()
        return build_orchestrator_command(request, active_registry)

    def prepare_project(
        self, request: OrchestratorRunRequest, definition: OrchestratorDefinition
    ) -> tuple[object | None, OrchestratorRunRequest]:
        return _prepare_project_request(request, definition)

    def resolve_project_request(
        self, request: OrchestratorRunRequest, definition: OrchestratorDefinition
    ) -> OrchestratorRunRequest:
        return _resolve_project_request(request, definition)

    def is_dry_run(self, request: OrchestratorRunRequest, definition: OrchestratorDefinition) -> bool:
        return bool(request.dry_run)

    def prepare_dry_run_request(
        self, request: OrchestratorRunRequest, definition: OrchestratorDefinition
    ) -> OrchestratorRunRequest:
        return _prepare_dry_run_request(request, definition)

    def run_inner(self, request: OrchestratorRunRequest, definition: OrchestratorDefinition) -> OrchestratorRunResult:
        return _run_orchestrator_inner(request, definition)

    def finalize_project(
        self,
        context: object,
        request: OrchestratorRunRequest,
        *,
        status: RunStatus,
        returncode: int | None,
        error: BaseException | str | None = None,
    ) -> None:
        _finalize_project_orchestrator(context, request, status=status, returncode=returncode, error=error)

    def status_for_result(self, result: OrchestratorRunResult) -> RunStatus:
        return _project_status_for_result(result)

    def result_returncode(self, result: OrchestratorRunResult) -> int | None:
        return result.returncode


_ORCHESTRATOR_RUNNER = OrchestratorCapabilityRunner()


def run_orchestrator(request: OrchestratorRunRequest, registry: OrchestratorRegistry | None = None) -> OrchestratorRunResult:
    return _ORCHESTRATOR_RUNNER.run(request, registry)



def _run_orchestrator_inner(request: OrchestratorRunRequest, orchestrator: OrchestratorDefinition) -> OrchestratorRunResult:
    values = _request_values(request, orchestrator)
    _validate_orchestrator_inputs(orchestrator, request)
    _validate_out_requirement(orchestrator, request)
    _validate_required_inputs(
        orchestrator.id, orchestrator.inputs, values, noun="orchestrator", error_cls=OrchestratorRunnerError
    )
    if orchestrator.runtime.kind == "python":
        return _ensure_dry_run_plan(_run_python_orchestrator(orchestrator, request))
    if orchestrator.runtime.kind == "command":
        return _ensure_dry_run_plan(_run_command_orchestrator(orchestrator, request, values))
    raise OrchestratorRunnerError(f"unsupported orchestrator runtime kind {orchestrator.runtime.kind!r}")


def build_orchestrator_command(request: OrchestratorRunRequest, registry: OrchestratorRegistry | None = None) -> tuple[str, ...]:
    active_registry = registry or load_default_registry()
    orchestrator = active_registry.get(request.orchestrator_id)
    values = _request_values(request, orchestrator)
    _validate_orchestrator_inputs(orchestrator, request)
    _validate_out_requirement(orchestrator, request)
    _validate_required_inputs(
        orchestrator.id, orchestrator.inputs, values, noun="orchestrator", error_cls=OrchestratorRunnerError
    )
    if orchestrator.runtime.kind != "command":
        raise OrchestratorRunnerError(f"orchestrator {orchestrator.id!r} does not use a command runtime")
    command, _, _ = _expand_command_runtime(orchestrator, request, values)
    return command


def _run_python_orchestrator(orchestrator: OrchestratorDefinition, request: OrchestratorRunRequest) -> OrchestratorRunResult:
    runtime = orchestrator.runtime
    if not runtime.module or not runtime.function:
        raise OrchestratorRunnerError(f"orchestrator {orchestrator.id!r} has an invalid Python runtime")
    if not request.dry_run:
        return _run_python_orchestrator_subprocess(orchestrator, request)


def _run_python_orchestrator_subprocess(
    orchestrator: OrchestratorDefinition,
    request: OrchestratorRunRequest,
) -> OrchestratorRunResult:
    """Execute a Python runtime target outside the runner process."""
    runtime = orchestrator.runtime
    root = Path(request.run_root or tempfile.mkdtemp(prefix="astrid-orchestrator-"))
    root.mkdir(parents=True, exist_ok=True)
    request_path = root / ".astrid-python-orchestrator.request"
    result_path = root / ".astrid-python-orchestrator.result"
    request_path.write_bytes(
        pickle.dumps(
            {
                "module": runtime.module,
                "function": runtime.function,
                "request": request,
                "orchestrator": orchestrator,
                "result_path": str(result_path),
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    )
    package_parent = str(Path(__file__).resolve().parents[4])
    existing_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = (
        package_parent
        if not existing_pythonpath
        else os.pathsep.join((package_parent, existing_pythonpath))
    )
    env = build_child_subprocess_env(
        parent=os.environ,
        explicit_env={
            "ASTRID_INTERNAL_INVOCATION": "1",
            "PYTHONPATH": pythonpath,
        },
        passthrough=orchestrator.isolation.env_passthrough,
        declared_passthrough=orchestrator.isolation.env_passthrough,
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "astrid.core.execution.python_runtime_worker", str(request_path)],
            stdin=subprocess.DEVNULL,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise OrchestratorRunnerError(
                f"orchestrator {orchestrator.id!r} Python runtime failed in subprocess"
                + (f": {detail[-3500:]}" if detail else "")
            )
        try:
            raw_result = pickle.loads(result_path.read_bytes())
        except (OSError, pickle.PickleError) as exc:
            raise OrchestratorRunnerError(
                f"orchestrator {orchestrator.id!r} subprocess returned no result"
            ) from exc
        return _normalize_python_result(orchestrator, request, raw_result)
    finally:
        request_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)
        if request.run_root in (None, ""):
            try:
                root.rmdir()
            except OSError:
                pass


def _run_command_orchestrator(
    orchestrator: OrchestratorDefinition,
    request: OrchestratorRunRequest,
    values: Mapping[str, Any],
) -> OrchestratorRunResult:
    command, cwd, env = _expand_command_runtime(orchestrator, request, values)
    if request.dry_run:
        planned_commands = (command,)
        return OrchestratorRunResult(
            orchestrator_id=orchestrator.id,
            kind=orchestrator.kind,
            runtime_kind="command",
            command=command,
            planned_commands=planned_commands,
            cwd=cwd,
            env=env,
            returncode=None,
            dry_run=True,
            plan=_plan_from_commands(planned_commands, prefix=orchestrator.id),
        )
    effective_env = _command_subprocess_env(orchestrator, request, env)
    run_root = request.run_root
    if orchestrator.metadata.get("local_orchestrator") is True:
        from astrid.core.execution.process_group import popen_owned_process, terminate_tree, verify_tree_absent
        process = None
        try:
            process = popen_owned_process(list(command), cwd=cwd, env=effective_env)
            returncode = process.wait()
            verify_tree_absent(process)
        except BaseException as error:
            if process is not None:
                error.process = process
                error.custody = getattr(process, "_astrid_custody", None)
                try:
                    terminate_tree(process)
                except BaseException as cleanup_error:
                    raise error from cleanup_error
            raise
    elif run_root is not None and not request.project_was_auto_resolved:
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
    return OrchestratorRunResult(
        orchestrator_id=orchestrator.id,
        kind=orchestrator.kind,
        runtime_kind="command",
        command=command,
        planned_commands=(command,),
        cwd=cwd,
        env=env,
        returncode=returncode,
    )


def _expand_command_runtime(
    orchestrator: OrchestratorDefinition,
    request: OrchestratorRunRequest,
    values: Mapping[str, Any],
) -> tuple[tuple[str, ...], str | None, dict[str, str]]:
    command_spec = orchestrator.runtime.command
    if command_spec is None:
        raise OrchestratorRunnerError(f"orchestrator {orchestrator.id!r} has no command runtime")
    placeholders = _placeholder_values(orchestrator, request, values)
    if orchestrator.metadata.get("local_orchestrator") is True:
        try:
            binding = expand_command(command_spec, orchestrator.inputs, {**values, **placeholders}, orchestrator.metadata)
            assert_provided_inputs_bound(binding, orchestrator.inputs, values, orchestrator.metadata)
        except BindingError as exc:
            raise OrchestratorRunnerError(str(exc)) from exc
        return tuple(binding.argv), binding.cwd, dict(binding.env)
    argv: list[str] = []
    for part in omit_absent_optional_flag_values(command_spec.argv, orchestrator.inputs, values):
        if part == "{orchestrator_args}":
            argv.extend(request.orchestrator_args)
        else:
            argv.append(
                _expand_placeholders(part, placeholders, error_cls=OrchestratorRunnerError)
            )
    cwd = (
        _expand_placeholders(command_spec.cwd, placeholders, error_cls=OrchestratorRunnerError)
        if command_spec.cwd
        else None
    )
    env = {
        key: _expand_placeholders(value, placeholders, error_cls=OrchestratorRunnerError)
        for key, value in command_spec.env.items()
    }
    return tuple(argv), cwd, env


def _validate_orchestrator_inputs(
    orchestrator: OrchestratorDefinition,
    request: OrchestratorRunRequest,
) -> None:
    """Reject SDK inputs that the runtime cannot consume.

    Command orchestrators only receive values represented by manifest
    placeholders.  Historically a caller could pass ``inputs`` to an
    orchestrator with no typed inputs (or with placeholders omitted) and the
    runner would silently drop them.  Fail before admission and point callers
    at the explicit ``orchestrator_args`` escape hatch.
    """
    provided = tuple(str(name) for name in request.inputs)
    declared = {port.name for port in orchestrator.inputs}
    unknown = sorted(set(provided) - declared)
    if unknown:
        declared_hint = ", ".join(sorted(declared)) or "none"
        raise OrchestratorRunnerError(
            f"orchestrator {orchestrator.id!r} does not declare SDK input(s): "
            f"{', '.join(unknown)}; declared inputs: {declared_hint}. "
            "recovery: pass the runtime flags through "
            "orchestrator_args=(\"--flag\", \"value\") and retry"
        )
    if not provided or orchestrator.runtime.kind != "command":
        return
    file_ports = {
        port.name
        for port in orchestrator.inputs
        if str(getattr(port, "type", "")).lower() == "file"
    }
    descriptor_inputs = sorted(
        name
        for name in provided
        if name in file_ports and isinstance(request.inputs[name], Mapping)
    )
    if descriptor_inputs:
        raise OrchestratorRunnerError(
            f"orchestrator {orchestrator.id!r} is a local command runner; file input(s) "
            f"{', '.join(descriptor_inputs)} must be local path strings, not managed "
            "descriptors; managed descriptors are only valid for child executor "
            "submissions"
        )
    command = orchestrator.runtime.command
    if command is None:
        return
    if orchestrator.metadata.get("local_orchestrator") is True:
        _expand_command_runtime(orchestrator, request, _request_values(request, orchestrator))
        return
    command_text = " ".join((*command.argv, command.cwd or "", *command.env.values()))
    placeholders = set(_PLACEHOLDER_RE.findall(command_text))
    dropped = sorted(name for name in provided if name not in placeholders)
    if dropped:
        raise OrchestratorRunnerError(
            f"orchestrator {orchestrator.id!r} declares input(s) that its command "
            f"does not consume: {', '.join(dropped)}; "
            "recovery: pass the runtime flags through "
            "orchestrator_args=(\"--flag\", \"value\") and retry"
        )


def _normalize_python_result(
    orchestrator: OrchestratorDefinition,
    request: OrchestratorRunRequest,
    raw_result: Any,
) -> OrchestratorRunResult:
    # Already a result — pass through with dry-run plan if needed.
    if isinstance(raw_result, OrchestratorRunResult):
        return _ensure_dry_run_plan(raw_result)

    # Rich dict normalisation (planned_commands, errors, plan, etc.).
    if isinstance(raw_result, dict):
        return _result_from_mapping(orchestrator, request, raw_result)

    # Delegate the remaining patterns (None, int, SystemExit, Mapping,
    # objects with a returncode attribute) to the shared helper.
    try:
        normalized = normalize_python_runtime_result(raw_result)
    except ValueError as exc:
        raise OrchestratorRunnerError(
            f"orchestrator {orchestrator.id!r} returned unsupported result "
            f"type {type(raw_result).__name__}; "
            "expected OrchestratorRunResult, dict, int, or None"
        ) from exc

    return _ensure_dry_run_plan(OrchestratorRunResult(
        orchestrator_id=orchestrator.id,
        kind=orchestrator.kind,
        runtime_kind="python",
        returncode=None if request.dry_run else normalized.returncode,
        dry_run=request.dry_run,
    ))


def _result_from_mapping(
    orchestrator: OrchestratorDefinition,
    request: OrchestratorRunRequest,
    raw: Mapping[str, Any],
) -> OrchestratorRunResult:
    command = _tuple_of_strings(raw.get("command", ()), "command")
    planned_commands = _planned_commands(raw.get("planned_commands", (command,) if command else ()))
    errors = tuple(
        error if isinstance(error, OrchestratorRunError) else OrchestratorRunError(str(error))
        for error in raw.get("errors", ())
    )
    returncode = raw.get("returncode")
    if request.dry_run and returncode is not None:
        returncode = None
    elif returncode is not None:
        returncode = int(returncode)
    plan = _plan_from_raw(raw.get("plan")) if "plan" in raw else None
    return _ensure_dry_run_plan(OrchestratorRunResult(
        orchestrator_id=str(raw.get("orchestrator_id") or orchestrator.id),
        kind=str(raw.get("kind") or orchestrator.kind),
        runtime_kind=str(raw.get("runtime_kind") or "python"),
        command=command,
        planned_commands=planned_commands,
        cwd=_optional_string(raw.get("cwd")),
        env={str(key): str(value) for key, value in dict(raw.get("env", {})).items()},
        returncode=returncode,
        dry_run=bool(raw.get("dry_run", request.dry_run)),
        outputs=dict(raw.get("outputs", {})),
        errors=errors,
        plan=plan,
    ))


def _ensure_dry_run_plan(result: OrchestratorRunResult) -> OrchestratorRunResult:
    if not result.dry_run or result.plan is not None:
        return result
    return replace(result, plan=_plan_from_commands(result.planned_commands, prefix=result.orchestrator_id))


def _plan_from_commands(commands: tuple[tuple[str, ...], ...], *, prefix: str) -> OrchestratorPlan:
    steps = tuple(
        OrchestratorPlanStep(
            id=f"{prefix}.step_{index + 1}",
            kind="command",
            command=command,
        )
        for index, command in enumerate(commands)
    )
    return OrchestratorPlan(steps=steps)


def _plan_from_raw(raw: Any) -> OrchestratorPlan | None:
    if raw is None:
        return None
    if isinstance(raw, OrchestratorPlan):
        return raw
    if not isinstance(raw, Mapping):
        raise OrchestratorRunnerError("plan must be an object")
    raw_steps = raw.get("steps", ())
    if not isinstance(raw_steps, (list, tuple)):
        raise OrchestratorRunnerError("plan.steps must be a list")
    return OrchestratorPlan(
        steps=tuple(_plan_step_from_raw(item, f"plan.steps[{index}]") for index, item in enumerate(raw_steps)),
        summary=str(raw.get("summary") or ""),
    )


def _plan_step_from_raw(raw: Any, path: str) -> OrchestratorPlanStep:
    if isinstance(raw, OrchestratorPlanStep):
        return raw
    if not isinstance(raw, Mapping):
        raise OrchestratorRunnerError(f"{path} must be an object")
    step_id = raw.get("id")
    if not isinstance(step_id, str) or not step_id.strip():
        raise OrchestratorRunnerError(f"{path}.id must be a non-empty string")
    return OrchestratorPlanStep(
        id=step_id,
        kind=str(raw.get("kind") or "command"),
        command=_tuple_of_strings(raw.get("command", ()), f"{path}.command"),
        description=str(raw.get("description") or ""),
        metadata=dict(raw.get("metadata", {})),
    )


def _placeholder_values(orchestrator: OrchestratorDefinition, request: OrchestratorRunRequest, values: Mapping[str, Any]) -> dict[str, str]:
    placeholders: dict[str, str] = {
        "python_exec": str(values.get("python_exec") or request.python_exec or sys.executable),
        "orchestrator_args": " ".join(request.orchestrator_args),
        "verbose": str(bool(values.get("verbose", request.verbose))).lower(),
    }
    # Kernel-owned invocations may deliberately omit the public ``out``
    # argument and provide their private attempt directory as ``run_root``.
    # Resolve both through one placeholder so command runtimes cannot escape
    # the kernel staging fence or fail merely because the public output path
    # is intentionally absent.
    effective_out = request.out if request.out not in (None, "") else request.run_root
    if effective_out is not None:
        placeholders["out"] = str(Path(effective_out).expanduser().resolve())
    brief = values.get("brief") or request.brief
    if brief is not None:
        placeholders["brief"] = str(Path(str(brief)).expanduser().resolve())
    for key, value in values.items():
        if value is None:
            continue
        if key == "verbose":
            placeholders[key] = str(bool(value)).lower()
        else:
            placeholders[key] = _stringify_value(value)
    for output in orchestrator.outputs:
        output_path = _output_value(output, request, placeholders, error_cls=OrchestratorRunnerError)
        placeholders[output.name] = output_path
        if output.placeholder:
            placeholders[output.placeholder] = output_path
    return placeholders


def _prepare_project_request(
    request: OrchestratorRunRequest,
    orchestrator: OrchestratorDefinition,
    ) -> tuple[object | None, OrchestratorRunRequest]:
    # Single-ledger cut: project runs are kernel-owned (RunRepository fan-out).
    # The runner retains output as staging only; no run.json is written here.
    # The kernel admission path (sdk.invoke / CapabilityTaskHandler) owns the
    # authoritative run/task ledger. Keep run directory as storage only.
    if not request.project:
        return None, request
    if not request.project_was_auto_resolved:
        reject_project_with_out(request.project, request.out)
    if _orchestrator_requires_output_path(orchestrator) and _has_cli_option(tuple(request.orchestrator_args), "--out"):
        raise OrchestratorRunnerError(
            f"--project cannot be combined with passthrough --out for {orchestrator.id}"
        )
    # A kernel worker may pass only its private attempt directory. It is the
    # equivalent of an explicit ``out`` for runtime expansion, but is not a
    # public user-selected output path and therefore must remain staging-only.
    if request.out in (None, "") and request.run_root not in (None, ""):
        return None, _request_with_effective_out(request, orchestrator, request.run_root)
    # No kernel run available and no explicit --out: fail closed.
    # The unified execution path requires a kernel run for every invocation;
    # storage-only run directories without a kernel row are not created.
    if request.out in (None, ""):
        raise ProjectRuntimeError(
            f"orchestrator {orchestrator.id!r} requires --out or a kernel run"
        )
    return None, request

def _orchestrator_requires_output_path(orchestrator: OrchestratorDefinition) -> bool:
    return bool(orchestrator.metadata.get("requires_output_path"))


def _request_with_effective_out(
    request: OrchestratorRunRequest,
    orchestrator: OrchestratorDefinition,
    out: str | Path,
) -> OrchestratorRunRequest:
    effective_out = Path(out).expanduser().resolve()
    args = tuple(request.orchestrator_args)
    if _orchestrator_requires_output_path(orchestrator) and not _has_cli_option(args, "--out"):
        args = (*args, "--out", str(effective_out))
    return replace(request, out=effective_out, orchestrator_args=args)


def _project_argv(request: OrchestratorRunRequest) -> list[str]:
    argv = ["orchestrators", "run", request.orchestrator_id]
    if request.project:
        argv.extend(["--project", request.project])
    if request.brief:
        argv.extend(["--brief", str(request.brief)])
    for key, value in request.inputs.items():
        argv.extend(["--input", f"{key}={_stringify_value(value)}"])
    if request.dry_run:
        argv.append("--dry-run")
    if request.python_exec:
        argv.extend(["--python-exec", request.python_exec])
    if request.verbose:
        argv.append("--verbose")
    if request.orchestrator_args:
        argv.append("--")
        argv.extend(request.orchestrator_args)
    return argv


def _project_status_for_result(result: OrchestratorRunResult) -> RunStatus:
    if result.dry_run:
        return RunStatus.SKIPPED
    if not result.ok:
        return RunStatus.FAILED
    return RunStatus.COMPLETED


def _finalize_project_orchestrator(
    context: object,
    request: OrchestratorRunRequest,
    *,
    status: RunStatus,
    returncode: int | None,
    error: BaseException | str | None = None,
) -> None:
    # Single-ledger cut: no authoritative run.json finalize here. The kernel
    # owns terminal status; this remains as a derived-projection hook (no-op
    # when called with a None context, which is the normal path).
    return


def _resolve_project_request(
    request: OrchestratorRunRequest,
    orchestrator: OrchestratorDefinition,
) -> OrchestratorRunRequest:
    project, source = selected_project(request.project)
    if source == "explicit":
        return request
    if project is not None:
        return replace(
            request,
            project=project,
            project_was_auto_resolved=True,
        )
    raise OrchestratorRunnerError(
        format_project_required_guidance(operation="orchestrator run")
    )


def _prepare_dry_run_request(
    request: OrchestratorRunRequest,
    orchestrator: OrchestratorDefinition,
) -> OrchestratorRunRequest:
    if request.out not in (None, ""):
        return _request_with_effective_out(request, orchestrator, request.out)
    placeholder = (
        Path.cwd() / ".astrid-dry-run" / request.orchestrator_id.replace(".", "-")
    ).resolve()
    return _request_with_effective_out(request, orchestrator, placeholder)


def _command_subprocess_env(
    orchestrator: OrchestratorDefinition,
    request: OrchestratorRunRequest,
    command_env: Mapping[str, str],
) -> dict[str, str]:
    project_env = _project_subprocess_env(request)
    return build_child_subprocess_env(
        parent={**os.environ, **project_env},
        explicit_env={
            **command_env,
            **project_env,
            "ASTRID_INTERNAL_INVOCATION": "1",
        },
        passthrough=orchestrator.isolation.env_passthrough,
        declared_passthrough=orchestrator.isolation.env_passthrough,
    )


def _validate_out_requirement(orchestrator: OrchestratorDefinition, request: OrchestratorRunRequest) -> None:
    if request.out not in (None, "") or request.run_root not in (None, ""):
        return
    if _orchestrator_requires_output_path(orchestrator):
        raise OrchestratorRunnerError(f"--out is required for {orchestrator.id}")
    if request.dry_run:
        return
    if orchestrator.runtime.kind == "command" and _command_runtime_requires_out(orchestrator, request):
        raise OrchestratorRunnerError("--out is required for command runtime placeholders")
    raise OrchestratorRunnerError("--out is required for orchestrator execution")


def _command_runtime_requires_out(orchestrator: OrchestratorDefinition, request: OrchestratorRunRequest) -> bool:
    command = orchestrator.runtime.command
    if command is None:
        return False
    values = [*command.argv]
    if command.cwd:
        values.append(command.cwd)
    values.extend(command.env.values())
    if any(_uses_placeholder(value, "out") for value in values):
        return True
    for output in orchestrator.outputs:
        if output.name in request.outputs or (output.placeholder and output.placeholder in request.outputs):
            continue
        if output.path_template is None or _uses_placeholder(output.path_template, "out"):
            return True
    return False


def _uses_placeholder(value: str, placeholder: str) -> bool:
    return placeholder in _PLACEHOLDER_RE.findall(value)


def _request_values(request: OrchestratorRunRequest, orchestrator: OrchestratorDefinition) -> dict[str, Any]:
    values = dict(request.inputs)
    for port in orchestrator.inputs:
        if port.default is not None and port.name not in values:
            values[port.name] = port.default
    if request.brief is not None and "brief" not in values:
        values["brief"] = request.brief
    if request.python_exec is not None and "python_exec" not in values:
        values["python_exec"] = request.python_exec
    values.setdefault("verbose", request.verbose)
    return values


def _planned_commands(raw: Any) -> tuple[tuple[str, ...], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise OrchestratorRunnerError("planned_commands must be a list of command lists")
    commands: list[tuple[str, ...]] = []
    for index, item in enumerate(raw):
        commands.append(_tuple_of_strings(item, f"planned_commands[{index}]"))
    return tuple(commands)


def _tuple_of_strings(raw: Any, path: str) -> tuple[str, ...]:
    if raw is None or raw == ():
        return ()
    if not isinstance(raw, (list, tuple)):
        raise OrchestratorRunnerError(f"{path} must be a list")
    result: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise OrchestratorRunnerError(f"{path}[{index}] must be a string")
        result.append(item)
    return tuple(result)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = [
    "OrchestratorCapabilityRunner",
    "OrchestratorRunError",
    "OrchestratorRunRequest",
    "OrchestratorRunResult",
    "OrchestratorRunnerError",
    "build_orchestrator_command",
    "run_orchestrator",
]
