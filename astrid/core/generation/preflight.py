"""Read-only readiness checks for generation backends.

The public generation facade performs these checks before handing work to the
kernel. Structural checks inspect local installation state; target selection
may probe one selected loopback endpoint. Neither starts ComfyUI, imports the
optional VibeComfy stack, creates staging directories, or writes target config.

Astrid's local adapter uses VibeComfy to start a managed ComfyUI server for a
one-shot run.  Therefore a live server endpoint is *not* a prerequisite for
the managed path and an offline endpoint must not be reported as an
unsupported installation.  Endpoint reachability belongs to the runtime
startup error, after admission, when the installation is otherwise ready.
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from astrid.packs.runpod.prepared_task import PreparedWorkerSelection


@dataclass(frozen=True)
class PreparedRunPodGeneration:
    """R03/F07 handoff; endpoint custody stays with the retained host owner.

    The published owner must use ensure_prepared_endpoint and
    EngineEndpointTransport for endpoint_checkout. A URL is never a handoff.
    """
    selection: PreparedWorkerSelection
    selection_path: str
    endpoint_checkout: Callable[..., Any]
    owner_tick: Callable[[Mapping[str, Any]], None]


class RunPodGenerationOwner(Protocol):
    """Typed fake boundary pending R04 and F05/F06/F07 publication.

    validation_target is read-only and must not claim/install. prepare checks
    a fresh offer/rate/consent and compatibility before beginning warm custody.
    monitor/retain own activation, timers, recovery and exact endpoint checkout.
    release dispatches runpod-release:<operation_id>; paid expiry cancels/fences
    the active task before release_prepared_worker(resume=True). Admission is
    never release proof: P1 quiescence + GET404 + list absence are required.
    """
    def validation_target(self) -> Any: ...
    def prepare(self, *, workflow: Any, inputs: Mapping[str, Any],
                consent: tuple[str, ...], max_usd: float, deadline: str) -> PreparedRunPodGeneration: ...
    def prepare_h3(self, *, inputs: Mapping[str, Any], consent: tuple[str, ...],
                   max_usd: float, deadline: str) -> PreparedRunPodGeneration: ...
    def validate_h3(self, inputs: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def describe(self) -> Mapping[str, Any]: ...
    def release(self, *, wait: bool) -> Any: ...
    def retain(self, task: Mapping[str, Any], monitor: Callable[..., None]) -> None: ...


def runpod_generation_owner(client: Any) -> RunPodGenerationOwner:
    owner = getattr(client, 'runpod_generation_owner', None)
    if owner is None:
        raise generation_error(ValueError('RunPod host owner is unavailable; R04/F05/F06/F07 publication is required'),
                               code='missing_dependency', details={'field': 'target'})
    return owner


def call_runpod_owner(method: Callable[..., Any], /, **kwargs: Any) -> Any:
    """Project new host-boundary failures without changing older SDK errors."""
    from astrid.sdk.exceptions import AstridSDKError
    try:
        return method(**kwargs)
    except (AstridError, AstridSDKError):
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        raise generation_error(exc) from exc

from astrid.core.contracts.errors import AstridError
from astrid.core.generation.interfaces import ReadinessEnvelope, ReadinessFix, ReadinessIssue


_CORE_CODES = {
    'COMFY_TARGET_UNAVAILABLE': 'target_unreachable',
    'COMFY_TARGET_INVALID': 'input_rejected',
    'COMFY_TARGET_UNSUPPORTED': 'missing_dependency',
    'COMFY_TARGET_NOT_READY': 'engine_not_found',
    'COMFY_TARGET_NO_MODEL': 'missing_dependency',
    'engine_not_installed': 'engine_not_found',
    'comfyui.missing_node': 'missing_dependency',
    'comfyui.models_missing': 'missing_dependency',
}


def readiness_envelope(*, entry=None, target=None, issues=(), fixes=()) -> ReadinessEnvelope:
    """Project observations once; unknown version facts do not become issues."""
    from copy import deepcopy
    issues = [{**issue, 'details': deepcopy(issue.get('details', {}))} for issue in issues]
    fixes = [{**fix, 'argv': list(fix['argv']), 'details': deepcopy(fix.get('details', {}))}
             for fix in fixes]
    status = ('blocked' if any(issue['severity'] == 'error' for issue in issues) else
              'ready_with_warnings' if issues else 'ready')
    return {'entry': deepcopy(entry),
            'target': deepcopy(target.as_dict() if hasattr(target, 'as_dict') else target),
            'status': status, 'issues': issues, 'fixes': fixes}


class GenerationReadinessError(AstridError, ValueError):
    """The same blocked payload is available to CLI, SDK and MCP adapters."""

    def __init__(self, envelope: ReadinessEnvelope, *, task_id: str | None = None):
        from astrid.core.contracts.errors import _redact_credential_data
        self.readiness = readiness_envelope(**{key: envelope[key] for key in
            ('entry', 'target', 'issues', 'fixes')})
        self.readiness = _redact_credential_data(self.readiness)
        self.task_id = task_id
        issue = self.readiness['issues'][0]
        fix = self.readiness['fixes'][0] if self.readiness['fixes'] else None
        import shlex
        recovery = shlex.join(fix['argv']) if fix else None
        if issue['code'] == 'generation_route_unqualified':
            recovery = issue['details'].get('fix') or recovery
        super().__init__(issue['message'], code=issue['code'],
                         field=issue['details'].get('field'),
                         recovery_command=recovery,
                         state_snapshot={'readiness': self.readiness})

    def to_envelope(self) -> dict[str, Any]:
        return {**self.readiness, 'status': 'blocked', 'task_id': self.task_id}


def generation_error(error: Any, *, entry=None, target=None, code=None,
                     details=None, fixes=(), task_id=None) -> GenerationReadinessError:
    """Normalize old engine/ExecError spellings at the generation boundary."""
    if isinstance(error, GenerationReadinessError):
        return error
    from collections.abc import Mapping
    if isinstance(error, Mapping) and {'entry', 'target', 'status', 'issues', 'fixes'} <= error.keys():
        return GenerationReadinessError(error, task_id=task_id or error.get('task_id'))
    task_id = task_id or getattr(error, 'task_id', None)
    readiness = getattr(error, 'readiness', None)
    if isinstance(readiness, dict):
        return GenerationReadinessError(readiness, task_id=task_id)
    get = error.get if isinstance(error, Mapping) else lambda key, default=None: getattr(error, key, default)
    category = get('category') or get('sdk_category')
    native = code or get('code') or {
        'precondition': 'missing_dependency', 'runtime': 'generation_failed',
    }.get(category, 'input_rejected')
    normalized = _CORE_CODES.get(native, native)
    detail = {**(get('details') or {}), **(details or {})}
    if normalized == 'generation_route_unqualified':
        detail['fix'] = get('fix') or get('recovery_command')
    for key in ('sdk_category', 'sdk_error'):
        if get(key):
            detail.setdefault(key, get(key))
    if normalized != native:
        detail['engine_code'] = native
    if get('field'):
        detail.setdefault('field', get('field'))
    message = get('message') or get('cause') or str(error)
    if normalized == 'input_rejected':
        import re
        match = re.search(r"(?:input|inputs for) '([^']+)'", message)
        if match:
            detail.setdefault('field', match[1])
        else:
            match = re.search(r'(?:unsupported entry inputs|missing required entry inputs|duplicate input values): (.+)', message)
            if match:
                names = match[1].split(', ')
                detail.setdefault('field', names[0])
                if len(names) > 1:
                    detail.setdefault('fields', names)
        detail.setdefault('constraint', message)
    issue = {'code': normalized, 'severity': 'error', 'message': message, 'details': detail}
    return GenerationReadinessError(readiness_envelope(entry=entry, target=target,
        issues=[issue], fixes=fixes), task_id=task_id)


def compare_versions(recorded: dict[str, Any], observed: dict[str, Any]) -> list[ReadinessIssue]:
    """Different known versions warn; unknown recorded/observed facts are silent."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version
    issues = []
    for name, version in recorded.items():
        current = observed.get(name)
        if not isinstance(version, str) or not isinstance(current, str):
            continue
        try:
            actual = Version(current)
            try:
                different = Version(version) != actual
            except InvalidVersion:
                constraint = SpecifierSet(version)
                if not str(constraint):
                    continue
                different = actual not in constraint
        except (InvalidVersion, InvalidSpecifier):
            continue
        if different:
            issues.append({'code': 'version_drift', 'severity': 'warning',
                           'message': f'{name} recorded version differs from the selected target',
                           'details': {'name': name, 'recorded': version, 'observed': current}})
    return issues


@dataclass(frozen=True)
class LocalGenerationReadiness:
    """A deterministic, side-effect-free local runtime readiness result."""

    ready: bool
    reason: str = ""
    recovery_command: str = ""
    entry: dict[str, Any] | None = None
    target: Any = None
    issues: tuple[ReadinessIssue, ...] = ()
    fixes: tuple[ReadinessFix, ...] = ()

    def to_envelope(self) -> ReadinessEnvelope:
        issues = self.issues
        if not self.ready and not issues:
            issues = ({'code': 'engine_not_found', 'severity': 'error',
                       'message': self.reason, 'details': {}},)
        return readiness_envelope(entry=self.entry, target=self.target,
                                  issues=issues, fixes=self.fixes)


def _feature_is_missing(feature: str, value: Any) -> bool:
    """Return whether a generation feature value is absent for preflight.

    Keep this small predicate here instead of importing the executor's
    ``_common`` module: preflight runs before capability admission and must
    remain independent of executor/runtime imports.
    """

    if value is None:
        return True
    if feature == "count":
        return not bool(value)
    if isinstance(value, str):
        return not value.strip()
    return False


def validate_generation_request(
    registry: Any,
    *,
    model: str,
    mode: str,
    execution: str,
    inputs: dict[str, Any] | None = None,
    modality: str | None = None,
    required_features: tuple[str, ...] | None = None,
) -> tuple[Any, Any]:
    """Validate a generation request before dry-run or kernel admission.

    This is the shared model → mode → execution matrix check used by typed
    generation facades and generic ``sdk.invoke``.  It also enforces the
    model-declared required inputs, notably ``image_end_ref`` for FLF video.
    The function performs no filesystem, network, runtime, or ledger writes.
    """

    from astrid.sdk.exceptions import (
        CapabilityMissingInputError,
        CapabilityValidationError,
    )

    if not isinstance(model, str) or not model.strip():
        raise CapabilityValidationError(
            "generation model must be a non-empty string"
        )
    if not isinstance(mode, str) or not mode.strip():
        raise CapabilityValidationError(
            "generation mode must be a non-empty string"
        )
    if not isinstance(execution, str) or not execution.strip():
        raise CapabilityValidationError(
            "generation execution must be one of the model's declared "
            "backends; provide execution='local' or execution='cloud'"
        )

    try:
        entry, mode_spec = registry.get_by_mode(model, mode)
    except (KeyError, TypeError, AttributeError) as exc:
        raise CapabilityValidationError(str(exc)) from exc

    if modality is not None and getattr(entry, "modality", None) != modality:
        raise CapabilityValidationError(
            f"model {model!r} is a {getattr(entry, 'modality', 'unknown')} "
            f"model, not a {modality} model"
        )

    backend_ids = tuple(sorted(getattr(mode_spec, "backends", {})))
    if execution not in backend_ids:
        available = ", ".join(backend_ids) or "none"
        raise CapabilityValidationError(
            f"Execution {execution!r} is not available for model {model!r} "
            f"mode {mode!r}. Available backends: {available}"
        )

    request_inputs = inputs or {}
    required = (
        getattr(mode_spec, "requires", ())
        if required_features is None
        else required_features
    )
    missing = [
        feature
        for feature in required
        if _feature_is_missing(feature, request_inputs.get(feature))
    ]
    if missing:
        joined = ", ".join(sorted(missing))
        pronoun = "it" if len(missing) == 1 else "them"
        raise CapabilityMissingInputError(
            f"model {model!r} mode {mode!r} requires: {joined}. "
            f"Provide {pronoun} before admission and retry."
        )

    from astrid.core.generation.availability import require_model_available
    require_model_available(model, mode, execution)
    return entry, mode_spec


def _module_available(module_name: str) -> bool:
    """Return whether *module_name* can be discovered without importing it."""

    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        # ``find_spec('comfy.cmd.main')`` raises when an optional parent package
        # is absent (and malformed __spec__ objects can raise ValueError).
        return False


def _comfyui_runtime_available() -> bool:
    """Return whether the managed ComfyUI runtime can be started locally."""

    if _module_available("comfy.cmd.main"):
        return True
    executable = shutil.which("comfyui")
    return bool(executable)


def _managed_install_fix() -> ReadinessFix | None:
    """Describe the indexed local installer for this host without running it."""
    import platform
    from astrid.core.generation.engine_contracts import EngineError
    from astrid.core.generation.engine_local_endpoint import default_engine_root
    from astrid.core.generation.engine_recipe import load_recipe_index, load_recipe_model_lock

    # The current local execution adapter supports Linux x86_64. Do not offer
    # its recipe on another machine, or infer a CUDA platform from the CPU pod.
    if platform.system() != 'Linux' or platform.machine().lower() not in {'x86_64', 'amd64'}:
        return None
    try:
        candidates = [row for row in load_recipe_index().entries
                      if row.engine == 'comfyui'
                      and row.platform.startswith('linux-x86_64-')]
        if len(candidates) != 1:
            return None
        recipe = candidates[0]
        root = default_engine_root(recipe.engine)
    except (EngineError, OSError):
        return None
    try:
        model_bytes = sum(model.bytes for model in load_recipe_model_lock(
            recipe.engine, recipe.platform).models)
    except (EngineError, OSError):
        model_bytes = None
    scope = f'installs Astrid-managed ComfyUI under {root}; user scope; no system changes'
    size = (f'at least {model_bytes} bytes of locked models; additional runtime download size unknown'
            if model_bytes is not None else 'size unknown')
    return {'argv': ['astrid', 'engines', 'install', recipe.engine,
                     '--platform', recipe.platform, '--placement', 'local'],
            'scope': 'managed_write',
            'details': {'bytes': model_bytes, 'size_scope': 'locked_models',
                        'additional_bytes': None, 'scope': scope,
                        'guidance': f'{scope}; {size}'}}


def check_local_generation_readiness(
    entry: Any,
    mode: str,
    *,
    python_executable: str | None = None,
    require_managed_runtime: bool = True,
) -> LocalGenerationReadiness:
    """Check deterministic prerequisites for a selected local model path.

    This is deliberately narrower than a runtime smoke test.  In particular,
    it does not probe a configured HTTP URL: Astrid starts a managed local
    server when the runtime is installed, and a configured/temporarily offline
    endpoint is a runtime condition rather than an unsupported installation.
    """

    mode_spec = getattr(entry, "modes", {}).get(mode)
    backend_spec = getattr(mode_spec, "backends", {}).get("local") if mode_spec else None
    template = getattr(backend_spec, "template", "") if backend_spec else ""
    if not isinstance(template, str) or not template.strip():
        return LocalGenerationReadiness(
            ready=False,
            reason=(
                f"local generation for model {getattr(entry, 'id', '<unknown>')!r} "
                f"mode {mode!r} has no configured VibeComfy template"
            ),
            recovery_command=(
                "inspect the model registry and configure a local template, "
                "or choose a declared cloud/codex backend"
            ),
        )

    if not _module_available("vibecomfy"):
        return LocalGenerationReadiness(
            ready=False,
            reason=(
                "local generation requires the 'vibecomfy' Python package; "
                "it is not installed in the Python environment Astrid is using"
            ),
            recovery_command=(
                "VibeComfy ships with Astrid; if it is missing, run the normal Astrid update "
                "in a terminal outside chat: "
                "curl -fsSL https://astrid.haus/install | sh -s -- --update (macOS/Linux); "
                "Windows PowerShell: & ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --update"
            ),
        )

    if require_managed_runtime and not _comfyui_runtime_available():
        install = _managed_install_fix()
        attach = ['astrid', 'generate', 'image', '--target', 'http://127.0.0.1:8188']
        import shlex
        guidance = f"start your existing ComfyUI and attach with {shlex.join(attach)}"
        if install:
            guidance = f"{shlex.join(install['argv'])} ({install['details']['guidance']}); or {guidance}"
        guidance += "; ComfyUI version provenance is unknown until the selected runtime is observed"
        return LocalGenerationReadiness(
            ready=False,
            reason=(
                "local generation requires a ComfyUI runtime ('comfy.cmd.main' "
                "or the 'comfyui' CLI); neither is installed or discoverable"
            ),
            recovery_command=guidance,
            fixes=(
                *((install,) if install else ()),
                {'argv': attach, 'scope': 'read_only',
                 'details': {'guidance': 'Attach after starting your existing ComfyUI'}},
            ),
        )

    return LocalGenerationReadiness(ready=True)


def require_local_generation_readiness(
    entry: Any,
    mode: str,
    *,
    python_executable: str | None = None,
) -> None:
    """Raise a public precondition error when local readiness is absent."""

    result = check_local_generation_readiness(
        entry,
        mode,
        python_executable=python_executable,
    )
    if result.ready:
        return

    from astrid.sdk.exceptions import CapabilityPreconditionError

    raise CapabilityPreconditionError(
        f"Local generation is not ready: {result.reason}. "
        f"Next: {result.recovery_command}"
    )


def _installed_managed_target():
    """Bind an installed ComfyUI through its existing receipt/custody verifier."""
    from .backends.comfy_target import ComfyTarget, ComfyTargetClient
    from .engine_contracts import EngineError
    from .engine_local_endpoint import local_engine_endpoint, recorded_engine_root
    from .targets import TargetRecord

    try:
        root = recorded_engine_root('comfyui')
        if root is None:
            return None
        installed = local_engine_endpoint('comfyui', root=root)
    except EngineError as exc:
        fix = _managed_install_fix()
        raise generation_error(
            ValueError('The installed managed ComfyUI engine is not ready; repair its installation.'),
            code='engine_not_found', details={'field': 'target', 'engine_code': exc.code},
            fixes=(() if fix is None else (fix,)),
        ) from exc
    endpoint = installed.endpoint
    facts = {'object_info_sha256': endpoint.health.object_info_sha256}
    client = ComfyTargetClient(ComfyTarget(endpoint.url, 'managed', facts))
    client.generation_target = TargetRecord(
        owner='astrid', endpoint=endpoint.url, root=root, layout='git',
        facts=facts, verified_at=endpoint.health.checked_at,
    )
    return client


def _select_generation_target(target: str, *, repo=None):
    """Select and probe one Comfy target without changing machine config.

    ``None`` selects managed lifecycle; a returned client owns only HTTP I/O.
    Only unbound discovery may fall back to the existing managed lifecycle.
    A saved or transient human address remains bound when unreachable.
    """
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
    from astrid.core.generation.targets import TargetConfigError, resolve_target

    if target == "runpod":
        raise AstridError(
            "RunPod generation requires an explicitly prepared target; automatic provisioning is unavailable",
            code="COMFY_TARGET_UNSUPPORTED", field="target",
            fix="Use --target managed inside an existing prepared pod, or --target auto locally",
        )
    try:
        selection = resolve_target(target, repo=repo)
        record = selection.record
        if record is None or record.owner == "astrid":
            return _installed_managed_target()
        if record.endpoint is None:
            raise AstridError(
                "Selected human ComfyUI target has no endpoint; launch it with explicit consent first",
                code="COMFY_TARGET_UNAVAILABLE", field="target",
                fix="Launch the selected ComfyUI and explicitly select its loopback URL",
            )
        url = record.endpoint
        client = ComfyTargetClient(ComfyTarget(url, "attached"))
    except GenerationReadinessError:
        raise
    except TargetConfigError as exc:
        raise AstridError(
            str(exc), code="COMFY_TARGET_INVALID", field="target",
            fix="Repair the saved ComfyUI config or explicitly select a loopback URL or managed target",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise AstridError(
            str(exc), code="COMFY_TARGET_INVALID", field="target",
            fix="Use --target auto, --target managed, or http://127.0.0.1:<port>",
        ) from exc
    try:
        facts = client.request_json("/system_stats", timeout=1.0)
        if not isinstance(facts, dict) or not isinstance(facts.get("system"), dict):
            raise ValueError("target did not return ComfyUI system facts")
    except (OSError, ValueError, TimeoutError) as exc:
        if not selection.bound:
            return None
        raise AstridError(
            f"ComfyUI target {url} is unavailable: {exc}",
            code="COMFY_TARGET_UNAVAILABLE", field="target",
            fix=f"Start ComfyUI at {url}, or use --target managed with an installed ComfyUI runtime",
            state_snapshot={'target': record.as_dict()},
        ) from exc
    client = ComfyTargetClient(ComfyTarget(client.target.url, "attached", {**record.facts, **facts}))
    from dataclasses import replace
    from datetime import datetime, timezone
    client.generation_target = replace(record, facts={**record.facts, **facts},
        verified_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
    return client


def select_generation_target(target: str, *, repo=None, runpod_owner: RunPodGenerationOwner | None = None):
    """Read/probe the bound target and expose the shared error envelope."""
    try:
        if target == 'runpod' and runpod_owner is not None:
            return call_runpod_owner(runpod_owner.validation_target)
        return _select_generation_target(target, repo=repo)
    except AstridError as exc:
        snapshot = exc.state_snapshot.get('target')
        selector = (snapshot or {}).get('endpoint') or target
        argv = ['astrid', 'generate', 'targets', '--target', selector]
        raise generation_error(exc, target=snapshot,
            fixes=[{'argv': argv, 'scope': 'read_only', 'details': {'guidance': exc.fix}}]) from exc


def require_generation_target_readiness(entry: Any, mode: str, client: Any) -> None:
    """Reuse package prerequisites while leaving server ownership to its owner."""
    from astrid.core.contracts.errors import AstridError

    readiness = check_local_generation_readiness(
        entry, mode, require_managed_runtime=client is None,
    )
    if not readiness.ready:
        error = AstridError(
            readiness.reason, code="COMFY_TARGET_NOT_READY", field="target",
            fix=(readiness.recovery_command + "; use --target http://127.0.0.1:8188 "
                 "when your existing ComfyUI is running there"),
        )
        raise generation_error(error,
            fixes=readiness.fixes or [
                {'argv': ['astrid', 'generate', 'targets', '--target', 'http://127.0.0.1:8188'],
                 'scope': 'read_only', 'details': {'guidance': readiness.recovery_command}}])
