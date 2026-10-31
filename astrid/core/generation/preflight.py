"""Read-only readiness checks for generation backends.

The public generation facade performs these checks before handing work to the
kernel.  They intentionally inspect only local installation state; they do
not start ComfyUI, import the optional VibeComfy stack, create staging
directories, or make a network request.

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
from typing import Any


@dataclass(frozen=True)
class LocalGenerationReadiness:
    """A deterministic, side-effect-free local runtime readiness result."""

    ready: bool
    reason: str = ""
    recovery_command: str = ""


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
        return LocalGenerationReadiness(
            ready=False,
            reason=(
                "local generation requires a ComfyUI runtime ('comfy.cmd.main' "
                "or the 'comfyui' CLI); neither is installed or discoverable"
            ),
            recovery_command=(
                "start your existing ComfyUI and use vibecomfy.run with its local server URL; "
                "see the VibeComfy skill for setup"
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


def select_generation_target(target: str):
    """Probe attached loopback before considering the existing managed owner.

    ``None`` selects managed lifecycle; a returned client owns only HTTP I/O.
    No installation or process lifecycle operation happens during selection.
    """
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient

    if target == "managed":
        return None
    if target == "runpod":
        raise AstridError(
            "RunPod generation requires an explicitly prepared target; automatic provisioning is unavailable",
            code="COMFY_TARGET_UNSUPPORTED", field="target",
            fix="Use --target managed inside an existing prepared pod, or --target auto locally",
        )
    url = "http://127.0.0.1:8188" if target == "auto" else target
    try:
        client = ComfyTargetClient(ComfyTarget(url, "attached"))
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
        if target == "auto":
            return None
        raise AstridError(
            f"ComfyUI target {url} is unavailable: {exc}",
            code="COMFY_TARGET_UNAVAILABLE", field="target",
            fix=f"Start ComfyUI at {url}, or use --target managed with an installed ComfyUI runtime",
        ) from exc
    return ComfyTargetClient(ComfyTarget(client.target.url, "attached", facts))


def require_generation_target_readiness(entry: Any, mode: str, client: Any) -> None:
    """Reuse package prerequisites while leaving server ownership to its owner."""
    from astrid.core.contracts.errors import AstridError

    readiness = check_local_generation_readiness(
        entry, mode, require_managed_runtime=client is None,
    )
    if not readiness.ready:
        raise AstridError(
            readiness.reason, code="COMFY_TARGET_NOT_READY", field="target",
            fix=(readiness.recovery_command + "; use --target http://127.0.0.1:8188 "
                 "when your existing ComfyUI is running there"),
        )
