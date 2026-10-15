"""Public SDK discovery and invocation helpers.

This module keeps invocation orchestration behind the SDK package boundary.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import platform
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrid.core.contracts.binding import (
    BindingError,
    assert_provided_inputs_bound,
    expand_command,
)

from .execution_request import (
    ExecutionRequest,
    ExecutionRequestError,
    merge_execution_input_manifest,
    merge_execution_request_inputs,
    normalize_execution_request,
    reject_caller_execution_binding,
)

from ._module import _sdk_module
from .exceptions import (
    AstridSDKError,
    CapabilityInvocationError,
    CapabilityMissingInputError,
    CapabilityPreconditionError,
    CapabilityValidationError,
    UnsupportedCapabilityError,
    _redact_message,
    _sdk_error_from_exception,
)
from .results import DiscoveryResult, InvocationResult, _json_safe, _json_safe_mapping


_MAX_GENERATION_METADATA_BYTES = 16 * 1024
_MAX_GENERATION_METADATA_DEPTH = 8
_INTERNAL_DISPATCH_TOKEN = object()


def _validate_generation_metadata(value: Any) -> dict[str, Any]:
    """Return a small JSON metadata object safe to persist with a generation."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CapabilityValidationError("generation_intent.metadata must be an object")

    def copy_json(item: Any, *, path: str, depth: int = 0) -> Any:
        if depth > _MAX_GENERATION_METADATA_DEPTH:
            raise CapabilityValidationError(
                f"{path} exceeds the maximum metadata nesting depth of "
                f"{_MAX_GENERATION_METADATA_DEPTH}"
            )
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise CapabilityValidationError(f"{path} must contain only finite JSON values")
            return item
        if isinstance(item, Mapping):
            copied: dict[str, Any] = {}
            for key, nested in item.items():
                if not isinstance(key, str) or not key:
                    raise CapabilityValidationError(
                        f"{path} keys must be non-empty strings"
                    )
                copied[key] = copy_json(nested, path=f"{path}.{key}", depth=depth + 1)
            return copied
        if isinstance(item, list):
            return [
                copy_json(nested, path=f"{path}[{index}]", depth=depth + 1)
                for index, nested in enumerate(item)
            ]
        raise CapabilityValidationError(
            f"{path} must contain only JSON objects, arrays, strings, numbers, booleans, or null"
        )

    copied = copy_json(value, path="generation_intent.metadata")
    try:
        encoded = json.dumps(
            copied,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise CapabilityValidationError(
            "generation_intent.metadata must be valid UTF-8 JSON"
        ) from exc
    if len(encoded) > _MAX_GENERATION_METADATA_BYTES:
        raise CapabilityValidationError(
            "generation_intent.metadata must be at most "
            f"{_MAX_GENERATION_METADATA_BYTES} bytes"
        )
    return copied


def _expanded_config_hash(config: Mapping[str, Any]) -> str:
    """Hash the exact in-memory expansion sent to the renderer."""
    payload = json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def discover(
    *,
    project_root: str | Path | None = None,
    extra_pack_roots: tuple[str, ...] = (),
    banodoco_config: Any | None = None,
    active_theme: str | Path | None = None,
    include_missing_roots: bool = False,
    kind: str | None = None,
) -> DiscoveryResult:
    sdk_module = _sdk_module()
    discovered_packs = sdk_module._discover_pack_inventory(
        project_root=project_root,
        extra_pack_roots=extra_pack_roots,
    )
    pack_permission_ids_by_pack_id = sdk_module._pack_permission_ids_by_pack_id(discovered_packs)
    executor_registry, orchestrator_registry, element_registry = sdk_module._load_registries(
        project_root=project_root,
        extra_pack_roots=extra_pack_roots,
        banodoco_config=banodoco_config,
        include_missing_roots=include_missing_roots,
        include_elements=True,
    )
    if element_registry is None:
        raise CapabilityInvocationError("element registry was not loaded")
    (
        packs,
        generation_backends,
        element_kinds,
        generation_features,
        generation_modes,
    ) = sdk_module._build_discovery_metadata(
        discovered_packs,
        element_registry=element_registry,
    )

    if pack_permission_ids_by_pack_id:
        executors = tuple(
            sdk_module._capability_from_executor(
                definition,
                executor_registry,
                pack_permission_ids_by_pack_id=pack_permission_ids_by_pack_id,
            )
            for definition in executor_registry.list()
        )
        orchestrators = tuple(
            sdk_module._capability_from_orchestrator(
                definition,
                orchestrator_registry,
                pack_permission_ids_by_pack_id=pack_permission_ids_by_pack_id,
            )
            for definition in orchestrator_registry.list()
        )
        elements = tuple(
            sdk_module._capability_from_element(
                definition,
                pack_permission_ids_by_pack_id=pack_permission_ids_by_pack_id,
            )
            for definition in element_registry.list()
        )
    else:
        executors = tuple(
            sdk_module._capability_from_executor(definition, executor_registry)
            for definition in executor_registry.list()
        )
        orchestrators = tuple(
            sdk_module._capability_from_orchestrator(definition, orchestrator_registry)
            for definition in orchestrator_registry.list()
        )
        elements = tuple(
            sdk_module._capability_from_element(definition)
            for definition in element_registry.list()
        )
    if kind is not None and kind not in ("action", "executor", "orchestrator", "element"):
        raise CapabilityValidationError(
            f"discover(kind=...) must be one of 'action', 'executor', 'orchestrator', "
            f"'element' — got {kind!r}"
        )
    executors = executors if kind in (None, "action", "executor") else ()
    orchestrators = orchestrators if kind in (None, "action", "orchestrator") else ()
    elements = elements if kind in (None, "element") else ()
    return DiscoveryResult(
        actions=executors + orchestrators,
        executors=executors,
        orchestrators=orchestrators,
        elements=elements,
        capabilities=executors + orchestrators + elements,
        packs=packs,
        generation_backends=generation_backends,
        element_kinds=element_kinds,
        generation_features=generation_features,
        generation_modes=generation_modes,
    )


def get_capability(
    capability_id: str,
    *,
    kind: Any | None = None,
    element_kind: str | None = None,
    project_root: str | Path | None = None,
    extra_pack_roots: tuple[str, ...] = (),
    include_elements: bool = True,
    banodoco_config: Any | None = None,
    active_theme: str | Path | None = None,
    include_missing_roots: bool = False,
    _registries: tuple[Any, Any, Any | None] | None = None,
):
    sdk_module = _sdk_module()
    if _registries is None:
        executor_registry, orchestrator_registry, element_registry = sdk_module._load_registries(
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
            banodoco_config=banodoco_config,
            include_missing_roots=include_missing_roots,
            include_elements=include_elements or kind == "element" or kind is None,
        )
    else:
        executor_registry, orchestrator_registry, element_registry = _registries

    resolved = sdk_module._resolve_capability(
        capability_id,
        kind=kind,
        element_kind=element_kind,
        executor_registry=executor_registry,
        orchestrator_registry=orchestrator_registry,
        element_registry=element_registry,
    )
    # Keep direct describes consistent with discover(): pack-level and
    # capability-specific safety permissions are part of the public handle,
    # not only of the full inventory DTO.
    discovered_packs = sdk_module._discover_pack_inventory(
        project_root=project_root,
        extra_pack_roots=extra_pack_roots,
    )
    return sdk_module._apply_pack_permission_ids(
        resolved,
        pack_permission_ids_by_pack_id=sdk_module._pack_permission_ids_by_pack_id(discovered_packs),
    )


def _normalize_executor_result(result: Any) -> dict[str, Any]:
    payload = {
        "executor_id": result.executor_id,
        "kind": result.kind,
        "command": result.command,
        "cwd": result.cwd,
        "env": result.env,
        "payload": result.payload,
        "returncode": result.returncode,
        "dry_run": result.dry_run,
        "skipped": result.skipped,
        "skipped_reason": result.skipped_reason,
        "missing_binaries": result.missing_binaries,
        "error": result.error,
        "ok": result.ok,
        "run_id": getattr(result, "run_id", None),
        "run_root": getattr(result, "run_root", None),
        "outputs": getattr(result, "outputs", {}),
        "executor_version": getattr(result, "executor_version", None),
    }
    return _json_safe_mapping(payload)


def _normalize_orchestrator_result(result: Any) -> dict[str, Any]:
    return _json_safe_mapping(result.to_dict())


def _validate_manifest_preview_inputs(
    capability: Any,
    *,
    inputs: Mapping[str, Any] | None,
    orchestrator_args: tuple[str, ...],
) -> dict[str, Any]:
    """Validate only manifest-owned inputs for a read-only invocation preview.

    Dry-run is deliberately a ledger/manifest operation.  It must not build a
    runner request (which imports project/run helpers), inspect an output tree,
    or resolve a local project.  Port requirements and declared orchestrator
    inputs are still checked here so callers retain the useful typed failures
    they receive from a live admission attempt.
    """

    values = dict(inputs or {})
    ports = tuple(getattr(capability, "inputs", ()) or ())
    # Defaults belong to the manifest ledger.  Include them in the effective
    # preview values without consulting runtime or project state.
    for port in ports:
        if port.name not in values and getattr(port, "default", None) is not None:
            values[port.name] = port.default
    from .actions import validate_action_inputs
    validate_action_inputs(capability, values)
    missing = [
        str(port.name)
        for port in ports
        if bool(getattr(port, "required", False))
        and getattr(port, "default", None) is None
        and values.get(port.name) in (None, "")
    ]
    if missing:
        raise CapabilityMissingInputError(
            f"{capability.capability_type} {capability.id!r} missing required input(s): "
            f"{', '.join(missing)}"
        )

    if capability.capability_type == "executor":
        metadata = capability.definition.get("metadata", {})
        choices = metadata.get("input_choices") if isinstance(metadata, Mapping) else None
        if isinstance(choices, Mapping):
            for input_name, raw_options in choices.items():
                if not isinstance(raw_options, (list, tuple)) or input_name not in values:
                    continue
                options = tuple(str(option) for option in raw_options)
                if str(values[input_name]) not in options:
                    rendered = ", ".join(options)
                    raise CapabilityValidationError(
                        f"invalid {input_name} {values[input_name]!r} for executor "
                        f"{capability.id!r}; valid options: {rendered}; recovery: retry with "
                        f"--{str(input_name).replace('_', '-')} <one of: {rendered}>"
                    )
        requirements = (
            metadata.get("input_requirements_by_choice")
            if isinstance(metadata, Mapping)
            else None
        )
        if isinstance(requirements, Mapping):
            for selector, choices_by_value in requirements.items():
                selected = values.get(str(selector))
                if selected is None or not isinstance(choices_by_value, Mapping):
                    continue
                required = choices_by_value.get(str(selected))
                if not isinstance(required, (list, tuple)):
                    continue
                missing_choice = [
                    str(name)
                    for name in required
                    if values.get(str(name)) in (None, "")
                ]
                if missing_choice:
                    raise CapabilityMissingInputError(
                        f"executor {capability.id!r} missing required input(s) for "
                        f"{selector}={selected!r}: {', '.join(missing_choice)}"
                    )
    else:
        declared = {str(port.name) for port in ports}
        unknown = sorted(set(values) - declared)
        if unknown:
            declared_hint = ", ".join(sorted(declared)) or "none"
            raise CapabilityValidationError(
                f"orchestrator {capability.id!r} does not declare SDK input(s): "
                f"{', '.join(unknown)}; declared inputs: {declared_hint}. recovery: pass the "
                "runtime flags through orchestrator_args=(\"--flag\", \"value\") and retry"
            )

    return _json_safe_mapping(values)


def _manifest_dry_run_result(
    capability: Any,
    *,
    inputs: Mapping[str, Any] | None,
    outputs: Mapping[str, Any] | None,
    brief: Path | str | None,
    python_exec: str | None,
    out: Path | str | None = None,
    orchestrator_args: tuple[str, ...] = (),
) -> tuple[dict[str, Any], bool]:
    """Build the stable no-side-effect preview envelope from a capability DTO."""

    validation_inputs = dict(inputs or {})
    if brief is not None:
        validation_inputs.setdefault("brief", brief)
    preview_inputs = _validate_manifest_preview_inputs(
        capability,
        inputs=validation_inputs,
        orchestrator_args=orchestrator_args,
    )
    if brief is not None:
        preview_inputs.setdefault("brief", _json_safe(brief))
    if python_exec is not None:
        preview_inputs.setdefault("python_exec", python_exec)
    preview = {
        "kind": "manifest-ledger",
        "capability_id": str(capability.id),
        "inputs": preview_inputs,
        "outputs": _json_safe_mapping(dict(outputs or {})),
    }
    command = _manifest_preview_command(
        capability,
        inputs=preview_inputs,
        outputs=outputs,
        brief=brief,
        python_exec=python_exec,
        out=out,
        orchestrator_args=orchestrator_args,
    )
    if capability.capability_type == "executor":
        return {
            "executor_id": capability.id,
            "kind": capability.native_kind,
            "command": command,
            "cwd": None,
            "env": {},
            "payload": {"preview": preview},
            "returncode": None,
            "dry_run": True,
            "skipped": False,
            "skipped_reason": "",
            "missing_binaries": [],
            "error": None,
            "ok": True,
            "run_id": None,
            "run_root": None,
            "outputs": {},
            "executor_version": None,
        }, True

    runtime = capability.definition.get("runtime", {})
    runtime_kind = runtime.get("kind") if isinstance(runtime, Mapping) else None
    return {
        "orchestrator_id": capability.id,
        "kind": capability.native_kind,
        "runtime_kind": runtime_kind or "unknown",
        "command": command,
        "planned_commands": [command] if command else [],
        "cwd": None,
        "env": {},
        "returncode": None,
        "dry_run": True,
        "outputs": {},
        "errors": [],
        "plan": {
            "steps": [],
            "summary": "manifest-ledger preview; execution deferred to the runtime",
        },
        "preview": preview,
        "ok": True,
    }, True


def _invoke_local_orchestrator(
    capability: Any,
    *,
    project: str | None,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any] | None,
    out: Path | str | None,
    brief: Path | str | None,
    python_exec: str | None,
    verbose: bool,
    orchestrator_args: tuple[str, ...],
) -> InvocationResult:
    """Run a parent orchestrator through the public SDK boundary.

    Parent orchestrators are launchers, not Runtime executor registrations.
    Their child executor calls still use the connected Runtime client from
    the orchestrator process.  Sending the parent itself to Runtime task
    admission would require the GenericPackHost to claim a second capability
    class and would lose the caller's local output root.
    """
    from astrid.core.execution.orchestrator.runner import (
        OrchestratorRunRequest,
        run_orchestrator,
    )

    request = OrchestratorRunRequest(
        orchestrator_id=str(capability.id),
        out=out,
        project=project,
        inputs=dict(inputs),
        outputs=dict(outputs or {}),
        brief=brief,
        python_exec=python_exec,
        verbose=verbose,
        # The public SDK has already resolved an explicit project and an
        # output root.  Mark it as resolved so the runner can retain the
        # caller-owned output directory while the child receives the project
        # identity through its normal environment/arguments.
        project_was_auto_resolved=True,
        invocation="sdk",
        run_root=out,
        orchestrator_args=tuple(orchestrator_args),
    )
    result = run_orchestrator(request)
    raw_result = _normalize_orchestrator_result(result)
    raw_result["dispatch"] = "public_sdk_local_orchestrator"
    raw_result["kernel_run_id"] = None
    raw_result["kernel_task_id"] = None
    raw_result["kernel_attempt_id"] = None
    error = None
    if not result.ok:
        errors = raw_result.get("errors")
        message = (
            str(errors[0].get("message"))
            if isinstance(errors, list) and errors and isinstance(errors[0], Mapping)
            else f"orchestrator {capability.id!r} failed"
        )
        error = {
            "code": "orchestrator_runtime",
            "message": message,
            "sdk_error": "OrchestratorRunnerError",
            "sdk_category": "runtime",
        }
    return InvocationResult(
        capability_id=capability.id,
        capability_type=capability.capability_type,
        native_kind=capability.native_kind,
        ok=bool(result.ok),
        error=error,
        manifest_path=None,
        raw_result=raw_result,
        run_id=None,
        run_root=str(Path(out).expanduser().resolve()) if out not in (None, "") else None,
        outputs=_json_safe_mapping(dict(result.outputs or {})),
        executor_version=None,
        kernel_run_id=None,
        kernel_task_id=None,
        kernel_attempt_id=None,
    )


def _manifest_preview_command(
    capability: Any,
    *,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any] | None,
    brief: Path | str | None,
    python_exec: str | None,
    out: Path | str | None = None,
    orchestrator_args: tuple[str, ...] = (),
) -> list[str]:
    """Expand a manifest command through the same lossless contract as execution."""

    definition = capability.definition
    ports = tuple(getattr(capability, "inputs", ()) or ())
    values: dict[str, Any] = {str(key): value for key, value in inputs.items()}
    for port in ports:
        if port.name not in values and getattr(port, "default", None) is not None:
            values[port.name] = port.default
    values.setdefault("brief", brief)
    values.setdefault("python_exec", python_exec or "python")
    values.setdefault("verbose", "false")
    if out not in (None, ""):
        values["out"] = out
    elif isinstance(outputs, Mapping) and "out" in outputs:
        values["out"] = outputs.get("out")
    else:
        # The runtime supplies each attempt's private staging directory; a
        # preview has none yet, so it shows where that value goes.
        values["out"] = "<runtime staging dir>"

    metadata = definition.get("metadata", {})
    append_pipeline_out = False
    if capability.capability_type == "executor":
        raw_command = definition.get("command")
        if not isinstance(raw_command, Mapping):
            module = metadata.get("runtime_module") if isinstance(metadata, Mapping) else None
            if not isinstance(module, str) or not module:
                return []
            argv: list[str] = ["{python_exec}", "-m", module]
            append_pipeline_out = out not in (None, "")
            raw_command = {"argv": argv}
    else:
        runtime = definition.get("runtime")
        raw_command = runtime.get("command") if isinstance(runtime, Mapping) else None
    if not isinstance(raw_command, Mapping):
        return []
    raw_argv = raw_command.get("argv")
    if not isinstance(raw_argv, (list, tuple)):
        return []
    brief_placeholders = ("{brief_out}", "{brief_copy}", "{brief_slug}")
    if values.get("brief") not in (None, "") and any(
        isinstance(part, str) and any(token in part for token in brief_placeholders)
        for part in raw_argv
    ):
        # Match the canonical executor builder's reserved brief values without
        # creating its output directory or running the executor.
        from astrid.core.execution.executor.runner import _default_brief_slug

        brief_path = Path(str(values["brief"])).expanduser().resolve()
        output_path = Path(str(values["out"]))
        if str(values["out"]) != "<runtime staging dir>":
            output_path = output_path.expanduser().resolve()
        brief_slug = str(values.get("brief_slug") or _default_brief_slug(brief_path, output_path))
        brief_out = output_path / "briefs" / brief_slug
        values.setdefault("brief_slug", brief_slug)
        values.setdefault("brief_out", str(brief_out))
        values.setdefault("brief_copy", str(brief_out / "brief.txt"))
    if "{orchestrator_args}" in raw_argv:
        flattened: list[Any] = []
        for part in raw_argv:
            if part == "{orchestrator_args}":
                flattened.extend(str(value) for value in orchestrator_args)
            else:
                flattened.append(part)
        raw_command = {**raw_command, "argv": flattened}
    try:
        result = expand_command(raw_command, ports, values, metadata)
        assert_provided_inputs_bound(result, ports, values, metadata)
    except BindingError as exc:
        if str(exc).startswith("missing mapped input"):
            raise CapabilityMissingInputError(
                f"capability {capability.id!r}: {exc}"
            ) from exc
        raise CapabilityValidationError(f"capability {capability.id!r}: {exc}") from exc
    command = list(result.argv)
    if append_pipeline_out:
        command.extend(("--out", str(values["out"])))
    return command


def _repeatable_string_input_names(capability: Any) -> frozenset[str]:
    """Return command-mapped string ports that explicitly accept repeats.

    This is intentionally narrower than the generic command binder's list
    handling: the public child boundary only admits repeated JSON strings when
    the admitted action declares that exact repeatable string mapping.
    """
    definition = getattr(capability, "definition", {})
    command = definition.get("command") if isinstance(definition, Mapping) else None
    if not isinstance(command, Mapping):
        return frozenset()
    input_args = command.get("input_args")
    if not isinstance(input_args, (list, tuple)):
        return frozenset()
    ports = {
        str(port.name): port
        for port in (getattr(capability, "inputs", ()) or ())
        if getattr(port, "name", None)
    }
    return frozenset(
        str(mapping["input"])
        for mapping in input_args
        if isinstance(mapping, Mapping)
        and type(mapping.get("input")) is str
        and mapping.get("repeatable") is True
        and mapping["input"] in ports
        and str(getattr(ports[mapping["input"]], "type", "")).lower() == "string"
    )


def _validate_timeline_visualize_inputs(
    inputs: Mapping[str, Any] | None,
    *,
    project: str | None,
    project_root: str | Path | None = None,
    out: str | Path | None = None,
    _client: Any | None = None,
) -> dict[str, Any]:
    """Validate visualization's selector/ownership contract before admission.

    This is intentionally read-only.  Timeline visualization's runner repeats
    the checks as defense-in-depth, but a public SDK call must reject a
    foreign, missing, or malformed timeline before kernel admission.
    """

    values = dict(inputs or {})
    if "revision_diff" in values:
        raise CapabilityValidationError("revision_diff is host-owned and cannot be supplied")
    if values.get("view") == "diff":
        if out not in (None, "") or not isinstance(project, str) or not project.strip():
            raise CapabilityValidationError("revision diff requires an explicit project and managed output")
        if (values.get("mode") == "inputs" or values.get("rendered_video") is not None
                or any(name in values for name in ("timeline_source", "filmstrip_authority", "transcript_file", "transcript.json"))):
            raise CapabilityValidationError("revision diff cannot substitute input-only or caller-rendered authority")
        compiled = _prepare_timeline_revision_diff(
            project=project, timeline_ref=values.get("timeline_slug") or values.get("timeline_ref"),
            before_revision_id=values.get("before_revision_id"), after_revision_id=values.get("after_revision_id"),
            _client=_client)
        return {"mode": "revision_diff", "revision_diff": compiled}
    raw_formats = values.get("formats")
    if raw_formats is not None:
        if isinstance(raw_formats, str):
            raw_formats = [raw_formats]
        if not isinstance(raw_formats, (list, tuple, set)):
            raise CapabilityValidationError(
                "rendering.timeline_visualize formats must be a list of png or md"
            )
        formats = {
            part.strip().lower()
            for token in raw_formats
            for part in str(token).split(",")
            if part.strip()
        }
        if not formats:
            raise CapabilityValidationError(
                "rendering.timeline_visualize formats must contain png or md"
            )
        allowed = {"png", "md"}
        invalid = sorted(formats - allowed)
        if invalid:
            raise CapabilityValidationError(
                f"invalid visualization format(s): {', '.join(invalid)}; "
                "choose png or md; the legacy SVG/all formats were removed"
            )
    if out not in (None, ""):
        raise CapabilityValidationError(
            "--out is not supported for project timeline visualization; "
            "omit it and use the returned durable manifest_path"
        )
    # Timeline files are authoring/migration inputs only.  A live product
    # invocation must address a runtime-owned timeline by its stable ref.
    if "timeline_source" in values:
        raise CapabilityValidationError(
            "timeline_source is not a supported product input; use timeline_slug "
            "or the runtime-selected default"
        )
    if "filmstrip_authority" in values:
        raise CapabilityValidationError("filmstrip_authority is host-owned and cannot be supplied")
    if "transcript_file" in values or "transcript.json" in values:
        raise CapabilityValidationError(
            "transcript input is host-owned; config.app.transcript supplies the CAS object"
        )
    from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import (
        inspection_options,
    )
    try:
        inspection_options(values)
    except ValueError as exc:
        raise CapabilityValidationError(str(exc)) from exc
    # The paired rendered filmstrip is the only public timeline visualization
    # surface.  The former structural diagram and frozen-object navigation
    # route were removed; all review navigation is render-scoped (range,
    # timestamp, shot, clip, asset, track, and density).
    view = values.get("view", "filmstrip")
    if view not in {"filmstrip", "contact", "motion"}:
        raise CapabilityValidationError(
            "view must be filmstrip, contact or motion; the structural timeline view was removed"
        )
    removed = [
        name for name in ("all", "from_view", "focus", "refresh_root", "layout", "filmstrip", "scope")
        if values.get(name) not in (None, "", False, [])
    ]
    if removed:
        flags = ", ".join(f"{name.replace('_', '-')}" for name in removed)
        raise CapabilityValidationError(
            f"legacy structural visualization options were removed ({flags}); "
            "use the filmstrip's --range/--at/--shot/--clip/--asset controls"
        )
    if not isinstance(project, str) or not project.strip():
        raise CapabilityValidationError("filmstrip review requires project=<slug>")
    if values.get("project_slug") not in (None, "", project):
        raise CapabilityValidationError("project_slug does not match project")
    from .timeline_filmstrip import prepare_filmstrip
    from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options
    try:
        filmstrip_options(values)
    except ValueError as exc:
        raise CapabilityValidationError(str(exc)) from exc
    context = prepare_filmstrip(values, project=project, client=_client)
    from copy import deepcopy
    # These snapshots are host-prepared immutable closures. Carry resource
    # identity in derived metadata; never rewrite their authored config.
    for key in ("capture_snapshot", "input_snapshot", "filmstrip_snapshot"):
        snapshot = context.get(key)
        if isinstance(snapshot, Mapping):
            snapshot = deepcopy(dict(snapshot))
            config = snapshot.get("config", snapshot)
            binding = _admitted_timeline_element_resources(config)
            snapshot.setdefault("metadata", {})["element_resources"] = binding
            snapshot["metadata"]["element_resource_binding_hash"] = _expanded_config_hash(binding)
            context[key] = snapshot
    return context


def _payload_manifest_path(raw_result: Mapping[str, Any]) -> str | None:
    payload = raw_result.get("payload")
    if not isinstance(payload, Mapping):
        return None
    for key in ("manifest_path", "manifest"):
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        path = Path(value).expanduser().resolve()
        if path.name == "manifest.json":
            return str(path)
    return None


_RENDER_PROFILE_REQUIRED_FIELDS = (
    "width",
    "height",
    "fps_rational",
    "time_base",
    "container",
    "video_codec",
    "video_profile",
    "video_level",
    "pixel_format",
    "duration_tolerance",
)
_RENDER_PROFILE_AUDIO_FIELDS = (
    "audio_codec",
    "audio_sample_rate",
    "audio_channel_layout",
)
_RENDER_PROFILE_ALLOWED_FIELDS = frozenset(
    (*_RENDER_PROFILE_REQUIRED_FIELDS, *_RENDER_PROFILE_AUDIO_FIELDS)
)
_RENDER_PROFILE_EXAMPLE = {
    "width": 1920,
    "height": 1080,
    "fps_rational": [30, 1],
    "time_base": [1, 90000],
    "container": "mp4",
    "video_codec": "h264",
    "video_profile": None,
    "video_level": None,
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "audio_sample_rate": 48000,
    "audio_channel_layout": "stereo",
    "duration_tolerance": 1,
}


def _render_profile_guidance() -> str:
    example = json.dumps(_RENDER_PROFILE_EXAMPLE, separators=(",", ":"))
    return (
        "--profile uses the flat RenderProfile v1 object (no video/audio nesting); "
        "audio_codec, audio_sample_rate, and audio_channel_layout must be supplied "
        "together or all omitted. Explicit profiles must match the authoritative "
        "theme canvas; set theme_overrides.visual.canvas for a different size. "
        f"Complete Remotion MP4 example: {example}"
    )


def _validate_explicit_render_profile(profile: Any) -> None:
    """Validate the frozen flat profile contract before kernel admission."""

    if profile is None:
        return
    if not isinstance(profile, Mapping):
        raise CapabilityValidationError(
            f"invalid render profile: expected a JSON object. {_render_profile_guidance()}"
        )
    missing = [field for field in _RENDER_PROFILE_REQUIRED_FIELDS if field not in profile]
    unknown = sorted(str(field) for field in profile if field not in _RENDER_PROFILE_ALLOWED_FIELDS)
    key_issues: list[str] = []
    if missing:
        key_issues.append("missing required field(s): " + ", ".join(missing))
    if unknown:
        key_issues.append("unknown field(s): " + ", ".join(unknown))
    if key_issues:
        raise CapabilityValidationError(
            "invalid render profile: " + "; ".join(key_issues) + ". " + _render_profile_guidance()
        )
    from astrid.core.rendering.contracts import RenderProfile

    try:
        RenderProfile.from_dict(profile)
    except (TypeError, ValueError) as exc:
        raise CapabilityValidationError(
            f"invalid render profile: {exc}. {_render_profile_guidance()}"
        ) from exc


def _validate_managed_profile_theme_compatibility(
    profile: Mapping[str, Any] | None,
    *,
    timeline: Mapping[str, Any],
    registry: Mapping[str, Any],
    timeline_slug: str,
) -> None:
    """Reject canvas/fps profiles that cannot match the canonical theme.

    This is intentionally managed-ref-only. Explicit file-mode callers retain
    the renderer's historical support-selection semantics.
    """

    if profile is None:
        return
    from astrid.core.rendering.profile import resolve_render_profile

    try:
        authoritative = resolve_render_profile(
            timeline,
            registry,
            audio_ownership="rendered",
        )
    except (TypeError, ValueError, OSError, FileNotFoundError) as exc:
        raise CapabilityValidationError(
            f"cannot resolve authoritative theme canvas for canonical timeline "
            f"{timeline_slug!r}: {exc}. Fix the timeline theme and retry"
        ) from exc

    mismatches: list[str] = []
    for field, expected in (
        ("width", authoritative.width),
        ("height", authoritative.height),
        ("fps_rational", list(authoritative.fps_rational)),
    ):
        requested = profile.get(field)
        if requested != expected:
            mismatches.append(
                f"{field}={requested!r} (authoritative theme canvas produces {expected!r})"
            )
    if mismatches:
        raise CapabilityValidationError(
            f"invalid render profile for canonical timeline {timeline_slug!r}: "
            + "; ".join(mismatches)
            + ". Explicit profiles must match the authoritative theme canvas; "
            "use the default profile from timelines render --help or set "
            "theme_overrides.visual.canvas to the requested width, height, and fps, then retry"
        )


def _validate_managed_speech_inputs(values: Mapping[str, Any]) -> None:
    """Validate frozen speech metadata before managed-render admission.

    Speech annotations and their render occurrences are an immutable render
    input contract.  Keep the projector as the single semantic validator so
    filmstrip preparation and render admission cannot disagree about digest,
    timing, or occurrence shape.  This helper deliberately does not discover
    transcript files or infer missing occurrences.
    """

    fields = (
        "speech_annotations",
        "transcript_annotations",
        "speech_occurrences",
        "source_audio_digest",
        "transcript_digest",
        "annotation_digest",
        "correction_version",
        "timing_method",
        "speech_coverage",
    )
    if not any(key in values and values[key] is not None for key in fields):
        return
    annotations = values.get("speech_annotations", values.get("transcript_annotations"))
    occurrences = values.get("speech_occurrences")
    if annotations is not None and not isinstance(annotations, list):
        raise CapabilityValidationError("speech_annotations must be a list of objects")
    if occurrences is not None and not isinstance(occurrences, list):
        raise CapabilityValidationError("speech_occurrences must be a list of objects")
    from astrid.packs.rendering.actions.timeline_visualize.speech_projection import (
        SpeechProjectionError,
        project_speech_annotations,
    )

    try:
        project_speech_annotations(
            annotations or [],
            occurrences or [],
            source_audio_digest=values.get("source_audio_digest"),
            transcript_digest=values.get("transcript_digest"),
            annotation_digest=values.get("annotation_digest"),
            correction_version=values.get("correction_version", 0),
            timing_method=values.get("timing_method"),
            coverage=values.get("speech_coverage"),
        )
    except SpeechProjectionError as exc:
        raise CapabilityValidationError(f"invalid frozen speech metadata: {exc}") from exc


def _validate_render_input_mode(inputs: Mapping[str, Any]) -> bool:
    """Select file mode without resolving scope or granting file authority."""
    for name in ("timeline_authority", "timeline_snapshot", "compiled_audio"):
        if name in inputs:
            raise CapabilityValidationError(
                f"caller-supplied {name} is not accepted; canonical render authority is host-owned"
            )
    file_mode = inputs.get("timeline") not in (None, "")
    if file_mode:
        if inputs.get("timeline_ref") not in (None, ""):
            raise CapabilityValidationError(
                "timeline and timeline_ref are mutually exclusive; use timeline for explicit "
                "file mode or timeline_ref for canonical managed mode"
            )
        if "expected_version" in inputs:
            raise CapabilityValidationError("expected_version is only valid with timeline_ref")
    return file_mode


_GIB = 1024**3


def _admitted_timeline_element_resources(config: Mapping[str, Any], *, registry=None, packs=None) -> dict[str, Any]:
    """Project declaration-owned resources beside immutable authored clips.

    The same admitted registry that chooses the rendering element resolves
    identity. No directory scan or resource basename supplies an owner.
    """
    from astrid.core.element.registry import load_default_registry
    from astrid.core.pack.discovery import discover_pack_metadata
    from astrid.core.foundation.paths import REPO_ROOT
    from copy import deepcopy
    clips = [clip for clip in config.get("clips", []) if isinstance(clip, Mapping)]
    candidates = [clip for clip in clips if isinstance(clip.get("elementRef"), Mapping)
                  or clip.get("clipType") not in (None, "", "video", "audio", "image", "shot")]
    if not candidates:
        return {}
    registry = registry or load_default_registry()
    if packs is None:
        packs = [row.pack for row in discover_pack_metadata(project_root=Path(REPO_ROOT))]
    bound = {}
    for clip in candidates:
        ref = clip.get("elementRef")
        explicit = isinstance(ref, Mapping)
        ref = ref if explicit else {"kind": "effects", "id": clip.get("clipType")}
        clip_id, element_id, kind = clip.get("id"), ref.get("id"), ref.get("kind")
        if not all(isinstance(value, str) and value for value in (clip_id, element_id, kind)):
            raise CapabilityValidationError("element resource binding requires exact clip/element/kind identities")
        try:
            element = registry.get(kind, element_id, pack_id=ref.get("packId"))
        except KeyError as exc:
            if not explicit:
                # Unregistered legacy clip types keep the existing truthful
                # mirror path; they cannot nominate a native motion module.
                continue
            raise CapabilityValidationError("element resource owner is not admitted") from exc
        pack_id = element.metadata.get("pack_id") or element.source.removeprefix("pack:")
        matching = [pack for pack in packs if pack.id == pack_id and
                    Path(element.root).resolve().is_relative_to(Path(pack.root).resolve())]
        if len(matching) != 1:
            raise CapabilityValidationError("element owner is absent or ambiguous in admitted declarations")
        pack = matching[0]
        declaration = pack.rendering.get(f"{element.kind}/{element.id}")
        if not isinstance(declaration, Mapping) or declaration.get("type") != "element":
            raise CapabilityValidationError("element has no exact V3 rendering declaration")
        root = Path(pack.root).resolve()
        def admitted_path(relative):
            relative = Path(relative)
            path = root / relative
            if (relative.is_absolute() or ".." in relative.parts or not path.is_file()
                    or any(part.is_symlink() for part in (path, *path.parents))
                    or not path.resolve().is_relative_to(root)):
                raise CapabilityValidationError("element resource is unavailable or outside its admitted pack")
            return path.resolve()
        manifest = admitted_path(declaration["path"])
        if manifest.parent != Path(element.root).resolve():
            raise CapabilityValidationError("element declaration differs from the admitted registry owner")
        resources = []
        declared_resources = [deepcopy(dict(row)) for row in declaration.get("resources", [])]
        for resource in declared_resources:
            path = admitted_path(resource["path"])
            if path.name != "motion.ts":
                continue
            if path.parent != manifest.parent or resource.get("kind") not in {"implementation", "support"}:
                raise CapabilityValidationError("motion resource is not declared by the same element")
            resources.append({"kind": resource["kind"], "path": str(path),
                              "declared_path": resource["path"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        bound[clip_id] = {"clip_id": clip_id,
                         "element_ref": {"packId": pack.id, "kind": element.kind, "id": element.id},
                         "declaration": {"pack_id": pack.id, "kind": element.kind, "id": element.id,
                                         "pack_root": str(root), "manifest_path": str(manifest),
                                         "resources": declared_resources},
                         "motion_resources": resources}
    return bound


def _prepare_timeline_revision_diff(*, project: str, timeline_ref: str,
                                    before_revision_id: str, after_revision_id: str,
                                    _client: Any) -> dict[str, Any]:
    """Compile two exact immutable authoring closures for admitted diff.

    This is an SDK preparation seam, not a public caller-snapshot port. The
    action remains fail-closed until its declared host-owned consumer is wired.
    """
    from copy import deepcopy
    from astrid.sdk.remote import RemoteTimelines
    for name, value in (("project", project), ("timeline_ref", timeline_ref),
                        ("before_revision_id", before_revision_id),
                        ("after_revision_id", after_revision_id)):
        if not isinstance(value, str) or not value.strip():
            raise CapabilityValidationError(f"revision diff requires explicit {name}")
    reader = RemoteTimelines(_client)
    sides = {}
    scope = None
    for side, revision in (("before", before_revision_id), ("after", after_revision_id)):
        opened = reader.open_bundle(project, timeline_ref, revision_id=revision)
        data = opened.data if opened.ok else None
        if not isinstance(data, Mapping) or not isinstance(data.get("bundle"), Mapping):
            raise CapabilityValidationError(f"cannot read exact {side} revision for diff")
        bundle = data["bundle"]
        parent = bundle.get("base_parent")
        current_scope = (data.get("project_id"), data.get("timeline_id"))
        if (not all(isinstance(value, str) and value for value in current_scope)
                or data.get("revision_id") != revision
                or bundle.get("project_id") != current_scope[0]
                or bundle.get("timeline_id") != current_scope[1]
                or not isinstance(parent, Mapping) or parent.get("revision_id") != revision
                or not isinstance(parent.get("content_digest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", parent["content_digest"].removeprefix("sha256:")) is None
                or len(parent["content_digest"].removeprefix("sha256:")) != 64
                or (scope is not None and scope != current_scope)):
            raise CapabilityValidationError("revision diff returned a foreign revision/project/timeline binding")
        scope = current_scope
        from astrid.packs.rendering.actions.render.managed_timeline import _project_exact_parent_head
        parent_row, projected, expansion = _project_exact_parent_head(
            client=_client, project_id=current_scope[0], timeline_id=current_scope[1], parent_revision_id=revision)
        if (parent_row.get("content_digest") != parent["content_digest"]
                or not isinstance(getattr(projected, "config", None), Mapping)
                or not isinstance(getattr(projected, "registry", None), Mapping)):
            raise CapabilityValidationError("revision diff projection differs from the exact immutable parent")
        pinned = deepcopy(dict(bundle))
        sides[side] = {"revision_id": revision, "parent_content_digest": parent["content_digest"],
                       "bundle_hash": _expanded_config_hash(pinned), "bundle": pinned,
                       "config": deepcopy(dict(projected.config)), "registry": deepcopy(dict(projected.registry)),
                       "config_hash": _expanded_config_hash(projected.config),
                       "registry_hash": _expanded_config_hash(projected.registry),
                       "expansion": deepcopy(expansion)}
    return {"schema_version": 1, "kind": "astrid.timeline_revision_diff.v1",
            "project_id": scope[0], "timeline_id": scope[1], **sides}


def _assert_render_disk_preflight(
    storage_estimate: Mapping[str, Any],
    *,
    volume: str | None = None,
) -> None:
    """Fail fast when the scratch volume cannot hold the render's envelope.

    The envelope is the admitted scratch plus output estimate, the same pair
    the Runtime and the pack host enforce. Checking it at admission turns a
    mid-render ENOSPC into an immediate, actionable error.
    """

    if "frame_sequence_bytes" not in storage_estimate:
        # Only the managed render estimate carries this model.
        return
    target = volume or tempfile.gettempdir()
    scratch_bytes = int(storage_estimate.get("estimated_scratch_bytes") or 0) or (
        int(storage_estimate["frame_sequence_bytes"])
        or int(storage_estimate.get("alpha_frame_working_bytes", 0))
    )
    output_bytes = int(storage_estimate["estimated_output_bytes"])
    required = scratch_bytes + output_bytes
    free = int(shutil.disk_usage(target).free)
    if required <= free:
        return
    frames = int(storage_estimate["duration_frames"])
    width = int(storage_estimate.get("width") or 0)
    height = int(storage_estimate.get("height") or 0)
    if storage_estimate.get("review_render"):
        options = "this is already a review render; render a shorter timeline or free disk space"
    else:
        options = "for a preview, render a review (640x360); or render a shorter timeline"
    raise CapabilityPreconditionError(
        f"rendering.render needs about {required / _GIB:.2f} GiB of scratch and output for "
        f"{frames} frames at {width}x{height}, but only {free / _GIB:.2f} GiB is free on "
        f"{target}. Options: {options}; or free disk space, or point TMPDIR at a larger "
        "volume before the pack host starts."
    )


def _prepare_managed_render_inputs(
    inputs: Mapping[str, Any] | None,
    *,
    project: str | None,
    _client: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Select admitted file inputs or resolve a canonical render snapshot.

    Snapshot bytes remain in the admission envelope until the generic host
    materializes them beneath the assigned attempt.
    """

    values = dict(inputs or {})
    if _validate_render_input_mode(values):
        # File mode carries existing CAS descriptors unchanged. Never open
        # these JSON inputs here or derive read authority from their contents;
        # declared file admission and host materialization own those bytes.
        from astrid.core.execution.managed_inputs import managed_file_digest

        for name in ("timeline", "assets_registry"):
            if values.get(name) in (None, ""):
                continue
            try:
                managed_file_digest(values[name], name)
            except ValueError as exc:
                raise CapabilityValidationError(str(exc)) from exc
        _validate_explicit_render_profile(values.get("profile"))
        values.pop("timeline_ref", None)
        return values, None
    timeline_ref = values.get("timeline_ref")
    expected_version = values.get("expected_version")
    if _client is not None:
        timeline_service = getattr(_client, "timelines", None)
        resolver = getattr(timeline_service, "resolve_scope", None)
        if callable(resolver):
            scope = resolver(project, timeline_ref)
            if not scope.ok or not isinstance(scope.data, Mapping):
                error = scope.error
                raise CapabilityPreconditionError(error.message)
            project = scope.data.get("project_ref") or project
            timeline_ref = scope.data.get("timeline_ref") or timeline_ref
            values["timeline_ref"] = timeline_ref
        elif timeline_ref in (None, ""):
            # Keep direct unit-test/older-client compatibility while all
            # connected RemoteAstridClient instances use the shared resolver.
            if project is None or not str(project).strip():
                project = _runtime_selected_project(_client)
            if project is None:
                raise CapabilityPreconditionError(
                    "no current project is selected; pass --project <project> or "
                    "select a current project in the workspace runtime"
                )
            shown = _client.projects.show(str(project))
            if not shown.ok:
                error = shown.error
                raise CapabilityPreconditionError(error.message)
            project_row = shown.data
            metadata = project_row.get("metadata") if isinstance(project_row, Mapping) else None
            default_ref = metadata.get("default_timeline_id") if isinstance(metadata, Mapping) else None
            if default_ref in (None, ""):
                raise CapabilityPreconditionError(
                    f"project {project!r} has no configured default canonical timeline; "
                    "pass a timeline ref or set metadata.default_timeline_id",
                )
            timeline_ref = str(default_ref)
            values["timeline_ref"] = timeline_ref
    if timeline_ref in (None, ""):
        raise CapabilityValidationError(
            "rendering.render requires timeline=<admitted Runtime file> or "
            "timeline_ref=<runtime timeline slug/UUID/ULID>"
        )
    if values.get("assets_registry") not in (None, ""):
        raise CapabilityValidationError(
            "assets_registry cannot be overridden with timeline_ref; the canonical timeline "
            "registry is pinned with the snapshot"
        )
    if project is None or not str(project).strip():
        raise CapabilityValidationError("rendering.render timeline_ref requires project=<slug>")
    if not isinstance(timeline_ref, str) or not timeline_ref.strip():
        raise CapabilityValidationError("timeline_ref must be a non-empty slug, UUID, or ULID")
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise CapabilityValidationError("expected_version must be a positive integer")
    _validate_explicit_render_profile(values.get("profile"))
    from astrid.packs.rendering.actions.render.managed_timeline import (
        ManagedRenderValidationError,
        _runtime_snapshot_registry,
        resolve_managed_render_snapshot,
        validate_managed_render_snapshot,
    )

    if _client is None:
        raise CapabilityInvocationError(
            "explicit generated runtime client is required for managed render admission"
        )
    try:
        authoring_preview = values.pop("authoring_preview", None)
        snapshot = resolve_managed_render_snapshot(
            project_ref=str(project),
            timeline_ref=timeline_ref.strip(),
            expected_version=expected_version,
            client=_client,
            candidate_preview=authoring_preview is not None,
        )
        if authoring_preview is not None:
            if not isinstance(authoring_preview, Mapping):
                raise ValueError("authoring_preview must be a frozen candidate artifact")
            from .authoring_render_preview import candidate_preview_snapshot

            snapshot = candidate_preview_snapshot(
                authoring_preview, snapshot=snapshot, client=_client
            )
        child_records: list[dict[str, Any]] = []
        review_shots: list[dict[str, Any]] = []
        review_phrases: list[dict[str, Any]] = []
        shot_occurrences: list[dict[str, Any]] = []
        shot_records: dict[tuple[str, str], dict[str, Any]] = {}
        review_bindings: dict[tuple[str, str], list[dict[str, Any]]] = {}
        from .remote import RemoteShots
        raw_clips = snapshot.config.get("clips", [])
        canonical_expansion = snapshot.expansion if isinstance(snapshot.expansion, Mapping) and snapshot.expansion.get("canonical") is True else None
        if canonical_expansion is not None:
            for shot in canonical_expansion.get("shots", []):
                if not isinstance(shot, Mapping) or not isinstance(shot.get("shot_id"), str):
                    continue
                shot_id = str(shot["shot_id"])
                revision_id = shot.get("revision_id")
                if not isinstance(revision_id, str) or not revision_id:
                    raise CapabilityValidationError(
                        f"canonical shot {shot_id!r} has no immutable revision identity"
                    )
                shot_key = (shot_id, revision_id)
                bindings = [dict(item) for item in shot.get("text_bindings", []) if isinstance(item, Mapping)]
                if values.get("review") is True:
                    verified_bindings = []
                    text_reader = getattr(_client, "shots", None)
                    if not callable(getattr(text_reader, "_text_binding_content", None)):
                        text_reader = RemoteShots(_client)
                    for binding in bindings:
                        if binding.get("kind") != "voiceover_script":
                            verified_bindings.append(binding)
                            continue
                        verified = text_reader._text_binding_content(binding)
                        if not verified.ok:
                            message = getattr(verified.error, "message", "pinned narration could not be verified")
                            raise CapabilityValidationError(str(message))
                        verified_bindings.append(dict(verified.data))
                    bindings = verified_bindings
                shot_records[shot_key] = {
                    "shot_id": shot_id,
                    "revision_id": revision_id,
                    "name": str(shot.get("name") or shot_id),
                    "version": revision_id,
                    "text_bindings": [{key: value for key, value in binding.items() if key != "text"} for binding in bindings],
                }
                review_bindings[shot_key] = bindings
            for occurrence in canonical_expansion.get("occurrences", []):
                if not isinstance(occurrence, Mapping):
                    continue
                shot_id = occurrence.get("shot_id")
                revision_id = occurrence.get("revision_id")
                occurrence_id = occurrence.get("occurrence_id")
                if (not isinstance(shot_id, str) or not isinstance(revision_id, str)
                        or not isinstance(occurrence_id, str)):
                    continue
                shot_key = (shot_id, revision_id)
                name = str(shot_records.get(shot_key, {}).get("name") or occurrence.get("name") or shot_id)
                at = float(occurrence.get("at", float(occurrence.get("at_ms", 0)) / 1000.0))
                hold = float(occurrence.get("hold", float(occurrence.get("duration_ms", 0)) / 1000.0))
                shot_occurrences.append({
                    "shot_occurrence_id": occurrence_id,
                    "shot_id": shot_id,
                    "name": name,
                    "at": at,
                    "hold": hold,
                    "timeline_document_id": str(occurrence.get("parent_document_id") or snapshot.timeline_id),
                    "source_index": int(occurrence.get("ordinal", len(shot_occurrences))),
                    "output_identity": occurrence.get("output_identity"),
                    "revision_id": revision_id,
                })
                review_shots.append({"shot_id": shot_id, "name": name, "at": at, "hold": hold})
                # The review caption is a phrase at a time, never the whole
                # chapter script: phrases are timed to the VO word timings
                # when present, else split and distributed over the shot.
                from astrid.core.timeline.review_captions import review_speech_phrases, vo_words_for_occurrence

                voice_bindings = [
                    binding for binding in review_bindings.get(shot_key, [])
                    if binding.get("kind") == "voiceover_script"
                    and isinstance(binding.get("text"), str) and binding["text"].strip()
                ]
                voice = max(voice_bindings, key=lambda item: int(item.get("head") or 0), default=None)
                review_phrases.extend(review_speech_phrases(
                    occurrence_id=occurrence_id,
                    shot_id=shot_id,
                    shot_start=at,
                    shot_end=at + hold,
                    words=vo_words_for_occurrence(raw_clips, occurrence_id),
                    script_text=voice["text"] if voice is not None else None,
                    binding=voice,
                ))

        if any(
            isinstance(clip, Mapping) and clip.get("clipType") == "shot"
            for clip in raw_clips
        ):
            raise CapabilityValidationError(
                f"canonical timeline {snapshot.timeline_slug!r} contains legacy clipType=shot entries; "
                "migrate the timeline with the offline migration utility before rendering"
            )

        expanded_config, expanded_registry = snapshot.config, snapshot.registry
        # The pure expander can carry the registered id and authored-order
        # occurrence through arbitrary child payloads.  Pin the name here,
        # after reading it from the canonical shot registry; child/caller
        # metadata can never forge review provenance.
        occurrence_names = {
            item["shot_occurrence_id"]: item["name"] for item in shot_occurrences
        }
        for flat_clip in expanded_config.get("clips", []):
            if not isinstance(flat_clip, Mapping):
                continue
            occurrence_id = flat_clip.get("shot_occurrence_id")
            if occurrence_id is None:
                continue
            if occurrence_id not in occurrence_names:
                raise CapabilityValidationError(
                    f"expanded clip {flat_clip.get('id', '?')} has an unknown shot occurrence"
                )
            flat_clip["shot_name"] = occurrence_names[occurrence_id]
        from dataclasses import replace

        expanded_registry = _runtime_snapshot_registry(
            expanded_registry,
            project_ref=str(project),
            client=_client,
        )
        snapshot = replace(
            snapshot,
            config=expanded_config,
            registry=expanded_registry,
            materialized_registry_hash=_expanded_config_hash(expanded_registry),
            expansion={
                "children": child_records,
                "shots": [shot_records[key] for key in sorted(shot_records)],
                "occurrences": shot_occurrences,
                "expanded_config_hash": _expanded_config_hash(expanded_config),
            },
        )
        validate_managed_render_snapshot(snapshot)
        snapshot = snapshot.compile_audio()
    except ManagedRenderValidationError as exc:
        raise CapabilityValidationError(str(exc), details=exc.details) from exc
    except ValueError as exc:
        raise CapabilityValidationError(str(exc)) from exc
    _validate_managed_speech_inputs(values)
    from .managed_transcript import transcript_input_from_snapshot

    try:
        transcript_input = transcript_input_from_snapshot(snapshot.config, snapshot.registry)
    except ValueError as exc:
        raise CapabilityValidationError(str(exc)) from exc
    if transcript_input is not None and any(
        values.get(key) is not None for key in ("speech_annotations", "transcript_annotations", "speech_occurrences")
    ):
        supplied_transcript_digest = values.get("transcript_digest")
        if supplied_transcript_digest != transcript_input["digest"]:
            raise CapabilityValidationError(
                "frozen speech transcript_digest must match config.app.transcript.sha256"
            )
    _validate_managed_profile_theme_compatibility(
        values.get("profile"),
        timeline=snapshot.config,
        registry=snapshot.registry,
        timeline_slug=snapshot.timeline_slug,
    )
    from astrid.core.rendering.output_policy import (
        DEFAULT_RENDER_OUTPUT_NAME,
        RenderOutputPolicyError,
        validate_render_output_policy,
    )

    output_name = values.get("output_name", DEFAULT_RENDER_OUTPUT_NAME)
    if output_name is None:
        output_name = DEFAULT_RENDER_OUTPUT_NAME
    try:
        validate_render_output_policy(
            output_name,
            timeline=snapshot.config,
            profile=values.get("profile"),
        )
    except RenderOutputPolicyError as exc:
        raise CapabilityValidationError(str(exc), details=exc.details) from exc
    # Never trust caller-authored review labels: pin registered names alongside the render.
    values.pop("review_context", None)
    # ``review`` still controls only burned-in visual labels.  The occurrence
    # envelope is always pinned at admission for provenance and script maps.
    values["review_context"] = {
        "shots": review_shots,
        "speech": {"status": "projected", "phrases": review_phrases},
        "element_resources": _admitted_timeline_element_resources(snapshot.config),
    }
    authority = snapshot.authority()
    values.update(
        {
            # The generic host materializes this immutable snapshot below the
            # assigned attempt.  The SDK never writes a project-side render
            # snapshot or hands a project-root locator to a renderer.
            "timeline_snapshot": {
                "config": dict(snapshot.config),
                "registry": dict(snapshot.registry),
                "compiled_audio": snapshot.compiled_audio,
            },
            "compiled_audio": snapshot.compiled_audio,
            "timeline_authority": authority,
        }
    )
    # The public selector and CAS guard are admission-only controls. The
    # resolved authority object below is the durable run input/cache identity;
    # do not leak these two controls as undeclared renderer CLI flags.
    values.pop("expected_version", None)
    return values, authority


def _discover_invocation_manifest_path(
    raw_result: Mapping[str, Any],
    *,
    out: Path | str | None,
) -> str | None:
    manifest_path = _payload_manifest_path(raw_result)
    if manifest_path is not None:
        return manifest_path
    outputs = raw_result.get("outputs")
    if isinstance(outputs, Mapping):
        output_manifest = outputs.get("manifest_path")
        if isinstance(output_manifest, str):
            candidate = Path(output_manifest).expanduser().resolve()
            if candidate.name == "manifest.json" and candidate.is_file():
                return str(candidate)
    roots: list[Path] = []
    for raw in (raw_result.get("run_root"), out):
        if raw in (None, ""):
            continue
        root = Path(str(raw)).expanduser().resolve()
        if root not in roots:
            roots.append(root)
    for root in roots:
        for candidate in (root / "manifest.json", root / "agent-view" / "manifest.json"):
            if candidate.is_file():
                return str(candidate)
    return None


def _cap_filmstrip_cache(raw_result: Mapping[str, Any], *, project: str | None) -> None:
    """Evict least-recently-used visualize captures above the cache cap.

    The capture just produced is protected, and the cap never fails the
    capture itself: a read-only or racing cache directory is left alone.
    """
    from . import visualize_cache

    outputs = raw_result.get("outputs")
    pack_root = outputs.get("pack_root") if isinstance(outputs, Mapping) else None
    if not isinstance(pack_root, str) or not pack_root:
        return
    produced = Path(pack_root)
    base = produced.parent.parent
    try:
        visualize_cache.touch_capture(produced)
        visualize_cache.prune_visualize_cache(base, protect=[produced])
    except (OSError, ValueError):
        return


def _visualize_cache_base(cache_root: Path | str | None = None) -> Path:
    """Return the root that holds every project's visualize captures."""
    if cache_root is not None:
        return Path(cache_root).expanduser().resolve()
    if (runtime_data_root := _runtime_data_root()) is not None:
        return runtime_data_root / "timeline-visualize"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "Astrid" / "timeline-visualize"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "astrid" / "timeline-visualize"


def _filmstrip_cache_parent(
    *,
    project: str | None,
    cache_root: Path | str | None = None,
) -> Path:
    """Return the durable, project-namespaced filmstrip cache parent."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(project or "unscoped")).strip("._") or "unscoped"
    return _visualize_cache_base(cache_root) / slug


def _runtime_data_root() -> Path | None:
    """Return the installation-owned data root used by the neutral runtime."""
    try:
        from .storage_root import resolve_runtime_data_root

        return resolve_runtime_data_root()
    except (ImportError, OSError, ValueError):
        # Older installed Astrid packages have no storage-root composition;
        # retain their cache behavior until the launcher is upgraded.
        return None


# Page files a visualize bundle may carry at its root, in reading order after
# the manifest's primary page: the one-page overview, motion sheets, then the
# numbered filmstrip pages.  Frames and input bands are evidence, not pages.
_FILMSTRIP_PAGE_PREFIXES = ("contact-sheet", "view-", "motion-", "filmstrip-")


def _filmstrip_pages(root: Path, document: Mapping[str, Any] | None) -> list[str]:
    """Readable pages of a verified filmstrip bundle, primary first."""
    entrypoints = document.get("entrypoints") if isinstance(document, Mapping) else None
    primary = entrypoints.get("png") if isinstance(entrypoints, Mapping) else None
    names = sorted(
        (path.name for path in root.glob("*.png") if path.name.startswith(_FILMSTRIP_PAGE_PREFIXES)),
        key=lambda name: (next(i for i, prefix in enumerate(_FILMSTRIP_PAGE_PREFIXES) if name.startswith(prefix)), name),
    )
    ordered: list[str] = []
    if isinstance(primary, str) and "/" not in primary and (root / primary).is_file():
        ordered.append(primary)
    ordered.extend(name for name in names if name not in ordered)
    return [str(root / name) for name in ordered]


def _filmstrip_sidecars(root: Path) -> dict[str, Any]:
    """Executor timing and text findings carried inside the bundle (small JSON/text files)."""
    found: dict[str, Any] = {}
    timing = root / "timing.json"
    if timing.is_file() and timing.stat().st_size < 65536:
        try:
            value = json.loads(timing.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = None
        if isinstance(value, Mapping):
            found["timing"] = dict(value)
    else:
        # Bundles from hosts that predate timing.json still say how many frames they hold.
        index = root / "frame-index.json"
        try:
            cards = json.loads(index.read_text(encoding="utf-8")).get("cards") if index.is_file() else None
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            cards = None
        if isinstance(cards, list):
            found["timing"] = {"frames": len(cards)}
    window = root / "window.json"
    if window.is_file() and window.stat().st_size < 65536:
        try:
            value = json.loads(window.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = None
        if isinstance(value, Mapping):
            found["window"] = dict(value)
    previews = sorted(root.glob("motion-*.gif"))
    if previews:
        found["preview"] = str(previews[0])
    findings = root / "findings.txt"
    if findings.is_file() and findings.stat().st_size < 262144:
        try:
            found["findings"] = [line for line in findings.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, UnicodeError):
            pass
    return found


def _materialize_filmstrip_outputs(
    raw_result: dict[str, Any],
    client: Any,
    *,
    project: str | None = None,
    cache_root: Path | str | None = None,
) -> str:
    """Rehydrate verified published evidence into a durable project cache."""
    import io
    import tempfile
    import zipfile
    from pathlib import PurePosixPath

    artifacts = raw_result.get("outputs", {}).get("artifacts", [])
    bundles = [a for a in artifacts if a.get("name") == "filmstrip_bundle"]
    if len(bundles) != 1:
        raise CapabilityInvocationError("filmstrip task did not publish its evidence bundle")
    artifact = bundles[0]
    digest = str(artifact.get("digest", "")).removeprefix("sha256:")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise CapabilityInvocationError("filmstrip bundle has an invalid digest")
    data = client.media.read_bytes("sha256:" + digest)
    if hashlib.sha256(data).hexdigest() != digest or len(data) != artifact.get("size"):
        raise CapabilityInvocationError("filmstrip bundle does not match its published digest and size")
    parent = _filmstrip_cache_parent(project=project, cache_root=cache_root)
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / digest
    staging = Path(tempfile.mkdtemp(prefix=f".{digest[:12]}-", dir=parent))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 10000 or sum(m.file_size for m in members) > 1024 ** 3:
                raise CapabilityInvocationError("filmstrip bundle exceeds evidence extraction limits")
            names: set[str] = set()
            for member in members:
                path = PurePosixPath(member.filename)
                if (path.is_absolute() or not path.parts or ".." in path.parts
                        or "\\" in member.filename or member.filename in names
                        or (member.external_attr >> 16) & 0o170000 == 0o120000):
                    raise CapabilityInvocationError("filmstrip bundle contains an unsafe or duplicate path")
                names.add(member.filename)
                destination = staging.joinpath(*path.parts)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(member))
        manifest = staging / "manifest.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        if document.get("kind") != "timeline_filmstrip" or not (staging / "frame-index.json").is_file():
            raise CapabilityInvocationError("filmstrip bundle is missing its manifest or frame-index entrypoint")
        declared = document.get("outputs")
        if not isinstance(declared, list):
            raise CapabilityInvocationError("filmstrip manifest lacks member integrity records")
        verified_members: set[str] = set()
        for member in declared:
            if not isinstance(member, Mapping):
                raise CapabilityInvocationError("filmstrip manifest has an invalid member")
            relative = member.get("path")
            if not isinstance(relative, str) or relative in verified_members:
                raise CapabilityInvocationError("filmstrip manifest has a duplicate or invalid path")
            member_path = PurePosixPath(relative)
            if member_path.is_absolute() or ".." in member_path.parts or "\\" in relative:
                raise CapabilityInvocationError("filmstrip manifest member escapes its bundle")
            path = staging.joinpath(*member_path.parts)
            if not path.is_file():
                raise CapabilityInvocationError("filmstrip manifest references a missing member")
            with path.open("rb") as stream:
                member_digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            if member_digest != member.get("content_hash") or path.stat().st_size != member.get("bytes"):
                raise CapabilityInvocationError("filmstrip bundle member integrity mismatch")
            verified_members.add(relative)
        actual_members = {p.relative_to(staging).as_posix() for p in staging.rglob("*") if p.is_file()}
        if actual_members != verified_members | {"manifest.json"}:
            raise CapabilityInvocationError("filmstrip bundle has unrecorded members")
        index_path = staging / "frame-index.json"
        try:
            frame_index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CapabilityInvocationError("filmstrip frame index is invalid") from exc

        def verified_relative_member(value: object, label: str) -> Path | None:
            if value is None:
                return None
            if not isinstance(value, str):
                raise CapabilityInvocationError(f"filmstrip {label} path is invalid")
            relative = PurePosixPath(value)
            if relative.is_absolute() or not relative.parts or ".." in relative.parts or "\\" in value:
                raise CapabilityInvocationError(f"filmstrip {label} path is unsafe")
            candidate = staging.joinpath(*relative.parts)
            if not candidate.is_file() or value not in verified_members:
                raise CapabilityInvocationError(f"filmstrip {label} path is not verified")
            return candidate

        media = frame_index.get("media") if isinstance(frame_index, Mapping) else None
        audio_sidecar = frame_index.get("audio_sidecar") if isinstance(frame_index, Mapping) else None
        media_path = verified_relative_member(media.get("path") if isinstance(media, Mapping) else None, "media")
        audio_path = verified_relative_member(audio_sidecar.get("path") if isinstance(audio_sidecar, Mapping) else None, "audio sidecar")
        media_relative = media_path.relative_to(staging) if media_path is not None else None
        audio_relative = audio_path.relative_to(staging) if audio_path is not None else None
        # Publish only after the archive and every declared member have been
        # verified. A successful retry replaces the prior cache atomically;
        # a failed retry leaves an existing usable cache untouched.
        if root.exists():
            import shutil
            shutil.rmtree(root)
        os.replace(staging, root)
        staging = root
        manifest = root / "manifest.json"
        pages = _filmstrip_pages(root, document)
        static_outputs = {
            "pack_root": str(root),
            "manifest_path": str(manifest),
            "pages": pages,
            "primary_page": pages[0] if pages else None,
            "frame_index": str(root / "frame-index.json"),
        }
        markdown = root / "filmstrip.md"
        if markdown.is_file():
            static_outputs["markdown"] = str(markdown)
        static_outputs.update(_filmstrip_sidecars(root))
        raw_result["outputs"].update(static_outputs)
        if media_relative is not None:
            raw_result["outputs"]["media"] = str(root / media_relative)
        if audio_relative is not None:
            raw_result["outputs"]["audio_analysis"] = str(root / audio_relative)
        return str(manifest)
    except Exception:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _invocation_outputs(
    raw_result: Mapping[str, Any],
    *,
    manifest_path: str | None,
    capability_id: str | None = None,
) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    declared = raw_result.get("outputs")
    if isinstance(declared, Mapping):
        outputs.update(declared)
    payload = raw_result.get("payload")
    if isinstance(payload, Mapping) and isinstance(payload.get("outputs"), Mapping):
        outputs.update(payload["outputs"])
    managed_outputs = raw_result.get("managed_outputs")
    if isinstance(managed_outputs, list):
        # These rows are Runtime-owned identities and remain valid after the
        # private attempt spool is removed. Do not substitute their filenames
        # with local paths; callers can request explicit materialization.
        outputs["managed_outputs"] = [
            _json_safe(item) for item in managed_outputs
            if isinstance(item, Mapping)
        ]
    reference_links = raw_result.get("reference_links")
    if isinstance(reference_links, list):
        outputs["reference_links"] = [_json_safe(item) for item in reference_links]
    if manifest_path is not None:
        manifest = Path(manifest_path)
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            document = None
        is_timeline_manifest = isinstance(document, dict) and document.get("kind") in {
            "timeline_visualize",
            "timeline_visualize_project",
        }
        if capability_id == "rendering.timeline_visualize" or is_timeline_manifest:
            pack_root = manifest.parent
            # Kernel completion publishes every evidence-pack member as its
            # own managed CAS object and removes private staging.  The parent
            # of the durable manifest is therefore a hash fan-out directory,
            # not the logical pack root.  Reuse the frozen loader's verified
            # task-output rehydration so the long-standing ``pack_root`` SDK
            # convenience remains an actually navigable directory.
            # The manifest is the runtime-owned result handle. Rehydration,
            # when needed for navigation, is persisted in the deterministic,
            # project-namespaced Astrid cache.
            outputs["pack_root"] = str(pack_root)
            outputs["manifest_path"] = str(manifest)
            if isinstance(document, dict) and document.get("kind") == "timeline_filmstrip":
                from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import action_argv
                outputs["inspection"] = action_argv(
                    "python3", "-m", "astrid", "timelines", "inspect", "--manifest", str(manifest), "--section", "summary"
                )
            is_filmstrip = isinstance(document, dict) and document.get("kind") == "timeline_filmstrip"
            if is_filmstrip:
                outputs["pages"] = _filmstrip_pages(pack_root, document)
                outputs["primary_page"] = outputs["pages"][0] if outputs["pages"] else None
                for key, value in _filmstrip_sidecars(pack_root).items():
                    outputs.setdefault(key, value)
            else:
                outputs["pages"] = [
                    str(path)
                    for path in sorted(pack_root.rglob("PG*.png"))
                    if "filmstrip" not in path.relative_to(pack_root).parts
                ]
            if is_filmstrip:
                markdown = pack_root / "filmstrip.md"
                if markdown.is_file():
                    outputs["markdown"] = str(markdown)
            outputs["file_hashes"] = {
                path.relative_to(pack_root).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in sorted(pack_root.rglob("*"))
                if path.is_file()
            }
    return _json_safe_mapping(outputs)


def _runtime_selected_project(client: Any | None = None) -> str | None:
    """Read the connected runtime's durable selection; never infer locally."""
    if client is None:
        return None
    current = getattr(getattr(client, "projects", None), "current", None)
    if not callable(current):
        raise CapabilityInvocationError("runtime client does not expose current project selection")
    result = current()
    if not result.ok:
        error = result.error
        if getattr(error, "code", None) == "not_found":
            return None
        from .exceptions import ServiceError, _SERVICE_ERROR_CLASSES
        error_type = _SERVICE_ERROR_CLASSES.get(error.code, ServiceError)
        raise error_type(error.message, details=error.details)
    data = result.data
    row = data.get("project") if isinstance(data, Mapping) else None
    if not isinstance(row, Mapping):
        raise CapabilityInvocationError("runtime current project returned an invalid selection")
    ref = row.get("project_id") or row.get("id") or row.get("slug")
    if not isinstance(ref, str) or not ref.strip():
        raise CapabilityInvocationError("runtime current project returned no project identity")
    return ref


def _project_scope(capability: Any) -> str:
    """Return validated executor project scope, defaulting old manifests to required."""
    definition = getattr(capability, "definition", None)
    metadata = definition.get("metadata", {}) if isinstance(definition, Mapping) else {}
    scope = metadata.get("project_scope", "required") if isinstance(metadata, Mapping) else "required"
    if scope not in {"required", "optional"}:
        raise CapabilityInvocationError(
            f"executor {getattr(capability, 'id', '<unknown>')!r} has invalid project_scope {scope!r}"
        )
    return str(scope)


def _validate_generation_intent(
    value: Any,
    *,
    modality: str,
) -> dict[str, Any]:
    """Validate and copy the opaque D1 generation intent envelope."""
    if not isinstance(value, Mapping):
        raise CapabilityValidationError("generation_intent must be an object")
    required_keys = {"version", "modality", "partial_success_policy", "groups"}
    allowed_keys = required_keys | {"metadata"}
    if not required_keys.issubset(value) or set(value) - allowed_keys:
        raise CapabilityValidationError(
            "generation_intent must contain version, modality, "
            "partial_success_policy, groups, and optional metadata"
        )
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise CapabilityValidationError("generation_intent.version must be 1")
    if value["modality"] != modality:
        raise CapabilityValidationError(
            f"generation_intent.modality must match generation modality {modality!r}"
        )
    partial_success_policy = value["partial_success_policy"]
    if not isinstance(partial_success_policy, str) or partial_success_policy not in {
        "reject", "allow"
    }:
        raise CapabilityValidationError(
            "generation_intent.partial_success_policy must be 'reject' or 'allow'"
        )
    groups = value["groups"]
    if not isinstance(groups, list):
        raise CapabilityValidationError("generation_intent.groups must be a list")
    if not groups:
        raise CapabilityValidationError(
            "generation_intent.groups must contain at least one group"
        )

    copied_groups: list[dict[str, Any]] = []
    seen_group_keys: set[str] = set()
    for group_index, group in enumerate(groups):
        if not isinstance(group, Mapping) or set(group) != {"group_key", "selectors"}:
            raise CapabilityValidationError(
                f"generation_intent.groups[{group_index}] must contain exactly "
                "group_key and selectors"
            )
        group_key = group["group_key"]
        if not isinstance(group_key, str) or not group_key.strip():
            raise CapabilityValidationError(
                f"generation_intent.groups[{group_index}].group_key must be non-empty"
            )
        if group_key in seen_group_keys:
            raise CapabilityValidationError(
                f"generation_intent has duplicate group_key {group_key!r}"
            )
        seen_group_keys.add(group_key)

        selectors = group["selectors"]
        if not isinstance(selectors, list):
            raise CapabilityValidationError(
                f"generation_intent.groups[{group_index}].selectors must be a list"
            )
        if not selectors:
            raise CapabilityValidationError(
                f"generation_intent.groups[{group_index}].selectors must contain "
                "at least one selector"
            )
        copied_selectors: list[dict[str, Any]] = []
        seen_ordinals: set[int] = set()
        seen_variant_keys: set[str] = set()
        for selector_index, selector in enumerate(selectors):
            if not isinstance(selector, Mapping) or not {
                "selector", "ordinal", "variant_key"
            }.issubset(selector) or set(selector) - {
                "selector", "ordinal", "variant_key", "required"
            }:
                raise CapabilityValidationError(
                    f"generation_intent.groups[{group_index}].selectors[{selector_index}] "
                    "must contain selector, ordinal, variant_key, and optional required"
                )
            selector_name = selector["selector"]
            ordinal = selector["ordinal"]
            variant_key = selector["variant_key"]
            if not isinstance(selector_name, str) or not selector_name.strip():
                raise CapabilityValidationError(
                    f"generation_intent.groups[{group_index}].selectors[{selector_index}] "
                    "selector must be non-empty"
                )
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise CapabilityValidationError(
                    f"generation_intent.groups[{group_index}].selectors[{selector_index}] "
                    "ordinal must be a non-negative integer"
                )
            if not isinstance(variant_key, str) or not variant_key.strip():
                raise CapabilityValidationError(
                    f"generation_intent.groups[{group_index}].selectors[{selector_index}] "
                    "variant_key must be non-empty"
                )
            required = selector.get("required", False)
            if not isinstance(required, bool):
                raise CapabilityValidationError(
                    f"generation_intent.groups[{group_index}].selectors[{selector_index}] "
                    "required must be a boolean"
                )
            if ordinal in seen_ordinals:
                raise CapabilityValidationError(
                    f"generation_intent group {group_key!r} has duplicate ordinal {ordinal}"
                )
            if variant_key in seen_variant_keys:
                raise CapabilityValidationError(
                    f"generation_intent group {group_key!r} has duplicate variant_key "
                    f"{variant_key!r}"
                )
            seen_ordinals.add(ordinal)
            seen_variant_keys.add(variant_key)
            copied_selector = {
                "selector": selector_name,
                "ordinal": ordinal,
                "variant_key": variant_key,
            }
            if "required" in selector:
                copied_selector["required"] = required
            copied_selectors.append(copied_selector)
        copied_groups.append({"group_key": group_key, "selectors": copied_selectors})

    result = {
        "version": 1,
        "modality": modality,
        "partial_success_policy": partial_success_policy,
        "groups": copied_groups,
    }
    if "metadata" in value:
        result["metadata"] = _validate_generation_metadata(value["metadata"])
    return result


def _generation_capability_modality(capability: Any) -> str | None:
    """Resolve a generation modality, including metadata opt-in capabilities."""
    capability_id = str(getattr(capability, "id", capability))
    if capability_id.startswith("generation.generate_image"):
        return "image"
    if capability_id.startswith("generation.generate_video"):
        return "video"
    if capability_id.startswith("generation.generate_audio"):
        return "audio"
    if capability_id in {"wan2gp.generate_video", "fal.h3_video", "vibecomfy.character_animation", "vibecomfy.video_enhance"}:
        return "video"
    if capability_id == "fal.fal_foley":
        return "audio"
    definition = getattr(capability, "definition", None)
    metadata = definition.get("metadata", {}) if isinstance(definition, Mapping) else {}
    publication = metadata.get("generation_publication") if isinstance(metadata, Mapping) else None
    modality = publication.get("modality") if isinstance(publication, Mapping) else None
    if modality in {"image", "video", "audio"}:
        return str(modality)
    return None


def _generation_primary_output_port(capability: Any, modality: str) -> str:
    """Resolve the single published file port for a typed generation route."""
    expected = {
        "image": "generated_images",
        "video": "generated_videos",
        "audio": "generated_audio",
    }[modality]
    declared_outputs = getattr(capability, "outputs", None)
    if not declared_outputs:
        definition = getattr(capability, "definition", None)
        declared_outputs = (
            definition.get("outputs", ())
            if isinstance(definition, Mapping)
            else getattr(definition, "outputs", ())
        )

    def output_field(output: Any, field: str) -> Any:
        if isinstance(output, Mapping):
            return output.get(field)
        return getattr(output, field, None)

    definition = getattr(capability, "definition", None)
    metadata = definition.get("metadata", {}) if isinstance(definition, Mapping) else getattr(definition, "metadata", {})
    publication = metadata.get("generation_publication") if isinstance(metadata, Mapping) else None
    if isinstance(publication, Mapping) and "output_port" in publication:
        port = publication["output_port"]
        selected = [output for output in declared_outputs or () if output_field(output, "name") == port]
        if (type(publication.get("version")) is not int or publication["version"] != 1
                or publication.get("modality") != modality
                or not isinstance(port, str) or not port.strip() or port != port.strip()
                or len(selected) != 1 or output_field(selected[0], "type") != "file"
                or port == "manifest" or port.endswith("_manifest")
                or output_field(selected[0], "artifact_type") == "metadata/result-manifest"):
            raise CapabilityValidationError("generation publication has an invalid explicit output declaration")
        return port
    if publication is not None and not isinstance(publication, Mapping):
        raise CapabilityValidationError("generation publication metadata must be a mapping")

    candidates = [
        str(output_field(output, "name"))
        for output in declared_outputs or ()
        if output_field(output, "type") == "file"
        and not str(output_field(output, "name") or "").endswith("_manifest")
        and output_field(output, "artifact_type")
    ]
    if candidates.count(expected) == 1:
        return expected
    if len(candidates) == 1:
        return candidates[0]
    untyped_candidates = [
        str(output_field(output, "name"))
        for output in declared_outputs or ()
        if output_field(output, "type") == "file"
        and not str(output_field(output, "name") or "").endswith("_manifest")
    ]
    if len(untyped_candidates) == 1:
        return untyped_candidates[0]
    raise CapabilityValidationError(
        f"generation capability must declare exactly one primary {modality!r} output"
    )


def _automatic_generation_intent(
    capability: Any,
    inputs: Mapping[str, Any],
    *,
    modality: str | None,
) -> dict[str, Any] | None:
    """Build a bounded default publication declaration for typed routes.

    Routes whose output cardinality comes from an opaque prompt file are left
    opt-in: admission cannot safely invent their selector set. Typed routes
    with a declared count, or a single fixed output, can use the canonical
    settlement path without requiring every caller to hand-compose D1 JSON.
    """
    if modality is None or getattr(capability, "capability_type", None) != "executor":
        return None
    capability_id = str(getattr(capability, "id", ""))
    if capability_id == "generation.generate_image_openai":
        return None

    count_value: Any = inputs.get("count")
    if count_value is None:
        for port in getattr(capability, "inputs", ()) or ():
            if getattr(port, "name", None) == "count":
                count_value = getattr(port, "default", None)
                break
    if count_value is None:
        count = 1
    else:
        try:
            count = int(count_value)
        except (TypeError, ValueError):
            return None
        if count < 1 or count > 128:
            return None
    selectors = []
    for ordinal in range(count):
        selectors.append({
            "selector": f"main-{ordinal}",
            "ordinal": ordinal,
            "variant_key": "original" if ordinal == 0 else f"variant-{ordinal}",
            "required": True,
        })
    return {
        "version": 1,
        "modality": modality,
        "partial_success_policy": "reject",
        "groups": [{"group_key": "main", "selectors": selectors}],
    }


def _generation_publish_effect(
    capability: Any,
    *,
    project: str | None,
    generation_intent: Mapping[str, Any],
    variant_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the sole typed publication effect from validated GEN intent.

    ``variant_context`` is resolved from Runtime before admission.  Keeping
    this branch here means every generation capability shares the same
    publication contract instead of teaching individual executors about
    generation lineage.
    """
    if not isinstance(project, str) or not project.strip():
        raise CapabilityValidationError(
            "generation publication requires a project-scoped task"
        )
    modality = generation_intent["modality"]
    output_port = _generation_primary_output_port(capability, modality)
    groups = []
    for group in generation_intent["groups"]:
        groups.append({
            "group_key": group["group_key"],
            "selectors": [
                {
                    "selector": selector["selector"],
                    "ordinal": selector["ordinal"],
                    "variant_key": selector["variant_key"],
                    "output_port": output_port,
                    **({"required": selector["required"]} if "required" in selector else {}),
                }
                for selector in group["selectors"]
            ],
        })
    payload = {
            "version": 1,
            "modality": modality,
            "generation_type": str(capability.id),
            "metadata": _validate_generation_metadata(generation_intent.get("metadata")),
            "partial_success_policy": generation_intent["partial_success_policy"],
            "groups": groups,
        }
    if variant_context is None:
        return {
            "effect_type": "generation.publish_v1",
            "target_id": project,
            "payload": payload,
        }

    selectors = [selector for group in groups for selector in group["selectors"]]
    if len(selectors) != 1:
        raise CapabilityValidationError(
            "variant generation requests must produce exactly one output"
        )
    selector = selectors[0]
    return {
        "effect_type": "generation.variant.append",
        "target_id": variant_context["generation_id"],
        "expected_version": variant_context["expected_version"],
        "payload": {
            "source_variant_id": variant_context["source_variant_id"],
            "source_object_id": variant_context["source_object_id"],
            "variant_type": "edit",
            "output_name": selector["output_port"],
            "output_ordinal": selector["ordinal"],
            "primary_policy": variant_context["primary_policy"],
        },
    }


def _domain_result_data(result: Any, *, operation: str) -> Any:
    """Unwrap a Runtime DomainResult at the generation admission boundary."""
    if isinstance(result, Mapping):
        ok = result.get("ok")
        data = result.get("data")
        error = result.get("error")
    else:
        ok = getattr(result, "ok", None)
        data = getattr(result, "data", None)
        error = getattr(result, "error", None)
    if ok is not True:
        message = getattr(error, "message", None)
        if message is None and isinstance(error, Mapping):
            message = error.get("message")
        raise CapabilityPreconditionError(
            f"{operation} failed: {message or 'Runtime returned no data'}"
        )
    return data


def _resolve_generation_variant(
    client: Any | None,
    *,
    project: str | None,
    variant_of: Any,
    primary: str,
) -> dict[str, Any]:
    """Resolve and authorize a Runtime generation variant for publication."""
    if not isinstance(variant_of, Mapping):
        raise CapabilityValidationError(
            "variant_of must be an object with generation_id and variant_id"
        )
    generation_id = variant_of.get("generation_id")
    variant_id = variant_of.get("variant_id")
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise CapabilityValidationError("variant_of.generation_id must be a non-empty string")
    if not isinstance(variant_id, str) or not variant_id.strip():
        raise CapabilityValidationError("variant_of.variant_id must be a non-empty string")
    if primary not in {"preserve", "promote"}:
        raise CapabilityValidationError("primary must be 'preserve' or 'promote'")
    if client is None:
        raise CapabilityPreconditionError(
            "variant generation requests require a connected Runtime client"
        )
    generations = getattr(client, "generations", None)
    show = getattr(generations, "show", None)
    variants = getattr(generations, "variants", None)
    if not callable(show) or not callable(variants):
        raise CapabilityPreconditionError(
            "connected Runtime client does not expose generation variant reads"
        )
    generation = _domain_result_data(
        show(project, generation_id), operation="generation lookup"
    )
    if not isinstance(generation, Mapping):
        raise CapabilityPreconditionError("generation lookup returned an invalid resource")
    owner = generation.get("project_id")
    if not isinstance(owner, str) or not owner:
        raise CapabilityPreconditionError("generation lookup returned no project owner")
    owner_matches = owner == project
    if not owner_matches:
        projects = getattr(client, "projects", None)
        project_show = getattr(projects, "show", None)
        if callable(project_show):
            project_row = _domain_result_data(
                project_show(project), operation="project lookup"
            )
            if isinstance(project_row, Mapping):
                owner_matches = owner in {
                    project_row.get("project_id"),
                    project_row.get("id"),
                    project_row.get("slug"),
                }
    if not owner_matches:
        raise CapabilityPreconditionError(
            "variant generation source belongs to a different project"
        )
    version = generation.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise CapabilityPreconditionError(
            "generation lookup returned no valid current version"
        )
    page = _domain_result_data(
        variants(project, generation_id), operation="generation variant lookup"
    )
    rows = page[0] if isinstance(page, (list, tuple)) and len(page) == 2 else page
    if not isinstance(rows, (list, tuple)):
        raise CapabilityPreconditionError("generation variant lookup returned an invalid page")
    source = next(
        (row for row in rows if isinstance(row, Mapping) and row.get("variant_id") == variant_id),
        None,
    )
    if source is None:
        raise CapabilityPreconditionError(
            f"generation variant {variant_id!r} was not found"
        )
    if source.get("generation_id") not in (None, generation_id):
        raise CapabilityPreconditionError("generation variant belongs to a different generation")
    source_object_id = source.get("object_id")
    if not isinstance(source_object_id, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", source_object_id
    ):
        raise CapabilityPreconditionError(
            "variant_of must reference a generation variant with a managed object"
        )
    return {
        "generation_id": generation_id,
        "expected_version": version,
        "source_variant_id": variant_id,
        "source_object_id": source_object_id,
        "primary_policy": primary,
    }


def _validate_variant_controls(
    variant_of: Any,
    primary: Any,
    *,
    modality: str | None,
    primary_supplied: bool = False,
) -> None:
    """Validate shared publication controls before model preflight."""
    if modality is None:
        if variant_of is None and not primary_supplied:
            return
        raise CapabilityValidationError(
            "variant_of and primary are only accepted for generation capabilities"
        )
    if variant_of is None and primary == "preserve":
        return
    if not isinstance(primary, str) or primary not in {"preserve", "promote"}:
        raise CapabilityValidationError("primary must be 'preserve' or 'promote'")
    if variant_of is None and primary == "promote":
        raise CapabilityValidationError("primary='promote' requires variant_of")


_AUTHORITY_SNAPSHOT_KEYS = frozenset({"filmstrip_snapshot", "input_snapshot", "capture_snapshot"})
_AUTHORITY_FILENAME = "filmstrip-authority.json"


def _import_json_object(importer: Any, project: str, data: bytes, filename: str) -> dict[str, Any]:
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    result = importer(
        project=project, data=data, filename=filename, media_type="application/json",
        idempotency_key="visualize-object-" + digest[7:39],
    )
    stored = getattr(result, "data", None) if getattr(result, "ok", False) else None
    if not isinstance(stored, Mapping):
        error = getattr(result, "error", None)
        raise CapabilityInvocationError(
            f"could not store {filename} for timeline visualization as a managed object: "
            f"{getattr(error, 'message', None) or error or 'no result'}"
        )
    if (stored.get("digest") or stored.get("object_id")) != digest:
        raise CapabilityInvocationError(f"stored {filename} does not match its digest")
    return {"digest": digest, "filename": filename, "media_type": "application/json", "size_bytes": len(data)}


def _store_visualize_objects(
    client: Any, project: str | None, authority_json: str, snapshot: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Store a visualize task's large host-owned documents as managed objects.

    Returns file-input descriptors: ``filmstrip_authority`` (the whole
    authority the executor reads), and, when the snapshot has them,
    ``timeline`` and ``assets_registry`` (the files the host used to derive
    from the inlined snapshot, with the same bytes). ``None`` means the
    client cannot import bytes and the caller keeps the inline form.
    """
    importer = getattr(getattr(client, "media", None), "import_bytes", None)
    if not callable(importer) or not project:
        return None
    stored = {"filmstrip_authority": _import_json_object(importer, str(project), authority_json.encode("utf-8"), _AUTHORITY_FILENAME)}
    for key, port, filename in (("config", "timeline", "visualize-timeline.json"),
                                ("registry", "assets_registry", "visualize-assets.json")):
        value = snapshot.get(key) if isinstance(snapshot, Mapping) else None
        if isinstance(value, Mapping):
            stored[port] = _import_json_object(importer, str(project), json.dumps(value, sort_keys=True).encode("utf-8"), filename)
    return stored


def _kernel_invoke(
    capability: Any,
    *,
    kind: Any,
    project: str | None,
    inputs: Mapping[str, Any] | None,
    outputs: Mapping[str, Any] | None,
    out: Path | str | None = None,
    brief: Path | str | None = None,
    python_exec: str | None = None,
    orchestrator_args: tuple[str, ...] = (),
    extra_pack_roots: tuple[str, ...] = (),
    idempotency_context: Mapping[str, Any] | None = None,
    admission_metadata: Mapping[str, Any] | None = None,
    generation_intent: Mapping[str, Any] | None = None,
    variant_context: Mapping[str, Any] | None = None,
    execution_request: Mapping[str, Any] | None = None,
    storage_estimate: Mapping[str, int] | None = None,
    child_delegation: Mapping[str, Any] | None = None,
    registry: Any | None = None,
    _client: Any | None = None,
    _recovery: Any | None = None,
    _recovery_metadata: Mapping[str, Any] | None = None,
    rerun: bool = False,
) -> tuple[str, str, str, Path | None, dict[str, Any], bool, Any]:
    """Admit an invocation through the runtime client and generic host.

    The SDK is a client of the workspace runtime.  It must not compose a
    local application, open SQLite, or execute a capability in-process on the
    normal invocation path. ``registry`` is an explicit dependency-injection
    seam for callers that provide test doubles and is intentionally unused by
    the runtime admission request.
    """
    del registry

    try:
        reject_caller_execution_binding(execution_request, None)
        execution_request = normalize_execution_request(execution_request)
    except ExecutionRequestError as exc:
        raise CapabilityValidationError(str(exc)) from exc

    request_inputs = dict(inputs or {})
    # Runtime workers expand the manifest command directly and therefore do
    # not see the public SDK's separate ``project=`` argument.  Mirror the
    # in-process executor runner's derivation for declared project_slug ports
    # so project-scoped executors receive the same explicit input on either
    # path.  Callers can still provide the field, subject to preflight's
    # project identity check.
    input_ports = {
        str(port.name)
        for port in (getattr(capability, "inputs", ()) or ())
        if getattr(port, "name", None)
    }
    if project and "project_slug" in input_ports and "project_slug" not in request_inputs:
        request_inputs["project_slug"] = project

    spec: dict[str, Any] = {
        "capability_id": str(capability.id),
        "kind": str(kind),
        "inputs": _json_safe_mapping(request_inputs),
        "outputs": _json_safe_mapping(dict(outputs or {})),
        "extra_pack_roots": list(extra_pack_roots),
    }
    if getattr(capability, "capability_type", str(kind)) == "orchestrator":
        # These are runner-owned fields, not creative inputs.  Keep them in
        # the admitted spec so a live Runtime worker can reconstruct the same
        # OrchestratorRunRequest that the dry-run command preview validated.
        if out not in (None, ""):
            spec["out"] = str(out)
        if brief not in (None, ""):
            spec["brief"] = str(brief)
        if python_exec not in (None, ""):
            spec["python_exec"] = str(python_exec)
        spec["orchestrator_args"] = [str(value) for value in orchestrator_args]
    try:
        spec = merge_execution_request_inputs(execution_request, spec)
    except ExecutionRequestError as exc:
        raise CapabilityValidationError(str(exc)) from exc
    if str(capability.id) == "vibecomfy.run":
        # Canonical sibling/source invocations use the same preflight as the
        # direct remote task route.  Convert request-owned descriptors into the
        # preflight view when callers supplied only the frozen request, and do
        # this before any task admission or worker work.
        canonical_names = {"python", "companion", "source", "source_video"}
        contract_inputs = (
            execution_request.get("inputs", [])
            if isinstance(execution_request, Mapping)
            else []
        )
        preflight_inputs = dict(request_inputs)
        if isinstance(contract_inputs, list):
            for item in contract_inputs:
                if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                    continue
                preflight_inputs.setdefault(
                    str(item["name"]),
                    {
                        "object_id": item.get("object_id"),
                        "digest": item.get("digest") or item.get("object_id"),
                        "filename": item.get("filename"),
                    },
                )
        if (
            canonical_names & set(preflight_inputs)
            or isinstance(execution_request, Mapping)
            and "workflow" in execution_request
        ):
            from .remote import _vibecomfy_invocation_preflight

            # ``invoke_result`` receives the public AstridClient, whose
            # Runtime object reader lives behind the two SDK composition
            # layers (AstridClient -> RemoteAstridClient -> WorkspaceClient).
            # Unwrap both layers before the canonical bundle preflight.  The
            # old one-layer lookup rejected every real public-client call
            # before admission, even though the reader was available.
            remote = getattr(_client, "_remote", _client)
            transport = getattr(remote, "_transport", remote)
            if not callable(getattr(transport, "get_object", None)):
                raise CapabilityValidationError(
                    "canonical VibeComfy preflight requires the runtime object reader"
                )
            try:
                spec["invocation_preflight"] = _vibecomfy_invocation_preflight(
                    transport,
                    {"inputs": preflight_inputs},
                    strict=True,
                )
            except Exception as exc:  # noqa: BLE001 - admission validation boundary
                raise CapabilityValidationError(
                    f"VibeComfy invocation preflight failed: {exc}"
                ) from exc
    if str(capability.id) == "generation.generate_image_codex":
        # Bounded host profiles consume typed params as their single input
        # authority, including the ordered CAS descriptors.
        spec["params"] = spec.pop("inputs")
    if idempotency_context:
        authority = dict(idempotency_context)
        if authority.get("authority_object"):
            # The full authority (with its snapshot) is a managed object; the
            # spec keeps the small fields and the object's digest.
            authority = {key: value for key, value in authority.items() if key not in _AUTHORITY_SNAPSHOT_KEYS}
        spec["authority_context"] = _json_safe_mapping(authority)
    if admission_metadata:
        # Keep the transparent estimate out of capability inputs: it is task
        # admission evidence, not an executor-authored input.
        spec["admission_metadata"] = _json_safe_mapping(dict(admission_metadata))
    # Managed renders authorize their snapshot registry media at admission:
    # derive task input_object_ids from the immutable timeline snapshot so
    # the generic host can materialize registry assets below the attempt.
    input_manifest: list[str] = []
    input_digests: list[dict[str, str]] = []
    # File ports are Runtime-owned CAS inputs on the task path.  Keep the
    # digest in both the explicit input-digest witness and the authorization
    # manifest; otherwise a remote GenericPackHost can see the descriptor but
    # is correctly forbidden from fetching it.  This was especially easy to
    # miss for VibeComfy's canonical sibling bundle and managed source video.
    file_input_names = {
        str(port.name)
        for port in (getattr(capability, "inputs", ()) or ())
        if getattr(port, "name", None)
        and str(getattr(port, "type", "")).lower() == "file"
    }
    for name in sorted(file_input_names):
        value = spec.get("inputs", request_inputs).get(name)
        if value is None:
            continue
        from astrid.core.execution.managed_inputs import managed_file_digest

        try:
            canonical = managed_file_digest(value, name)
        except ValueError as exc:
            raise CapabilityValidationError(str(exc)) from exc
        if canonical not in input_manifest:
            input_manifest.append(canonical)
        input_digests.append({"name": name, "digest": canonical})
    if input_digests:
        spec["input_digests"] = input_digests
    if variant_context is not None:
        source_object_id = variant_context.get("source_object_id")
        if not isinstance(source_object_id, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", source_object_id
        ):
            raise CapabilityValidationError(
                "variant generation source object must be a managed SHA-256 digest"
            )
        input_manifest.append(source_object_id)
    if str(capability.id) == "generation.generate_image_codex":
        # ``input_manifest`` already contains every declared file port,
        # including these Codex reference roles.  Use a separate set for the
        # role-level distinctness check; consulting ``input_manifest`` here
        # makes the first reference appear to duplicate itself and rejects
        # every valid Codex reference invocation.
        seen_reference_digests: set[str] = set()
        codex_reference_digests: list[str] = []
        for name in ("image_ref", "style_ref", "brand_ref"):
            reference = request_inputs.get(name)
            if reference is None:
                continue
            if not isinstance(reference, Mapping):
                raise CapabilityValidationError(f"{name} requires a managed image descriptor")
            digest = str(reference.get("digest") or "").removeprefix("sha256:")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise CapabilityValidationError(f"{name} requires a managed SHA-256 digest")
            canonical_digest = "sha256:" + digest
            if canonical_digest in seen_reference_digests:
                raise CapabilityValidationError("Codex reference roles must use distinct images")
            seen_reference_digests.add(canonical_digest)
            codex_reference_digests.append(canonical_digest)
        if codex_reference_digests:
            codex_reference_set = set(codex_reference_digests)
            input_manifest = [
                digest for digest in input_manifest
                if digest not in codex_reference_set
            ] + codex_reference_digests
    raw_snapshot = request_inputs.get("timeline_snapshot")
    if isinstance(raw_snapshot, Mapping):
        raw_registry = raw_snapshot.get("registry")
        raw_assets = raw_registry.get("assets") if isinstance(raw_registry, Mapping) else None
        if isinstance(raw_assets, Mapping):
            seen: set[str] = set()
            for entry in raw_assets.values():
                if not isinstance(entry, Mapping):
                    continue
                candidate = next(
                    (
                        value
                        for value in (
                            entry.get("object_id"),
                            entry.get("media_id"),
                            entry.get("content_sha256"),
                            entry.get("digest"),
                            entry.get("sha256"),
                            entry.get("hash"),
                        )
                        if isinstance(value, str)
                        and len(value.removeprefix("sha256:")) == 64
                        and all(
                            ch in "0123456789abcdef"
                            for ch in value.removeprefix("sha256:")
                        )
                    ),
                    None,
                )
                if candidate is None:
                    continue
                normalized = candidate.removeprefix("sha256:")
                if normalized in seen:
                    continue
                seen.add(normalized)
                input_manifest.append(candidate)

    # Timeline visualization carries its immutable snapshot in the
    # host-owned authority context rather than in public ``inputs``.  The
    # registry media still needs to be part of the admission manifest so the
    # generic host can fetch and hash-check those objects before the child
    # renders source previews.
    if (str(capability.id) == "rendering.timeline_visualize"
            and isinstance(idempotency_context, Mapping)
            and idempotency_context.get("mode") in {"filmstrip", "input_only", "composed_capture"}):
        authority_snapshot = (
            idempotency_context.get("filmstrip_snapshot")
            or idempotency_context.get("input_snapshot")
            or idempotency_context.get("capture_snapshot")
        )
        authority_registry = (
            authority_snapshot.get("registry")
            if isinstance(authority_snapshot, Mapping)
            else None
        )
        authority_assets = (
            authority_registry.get("assets")
            if isinstance(authority_registry, Mapping)
            else None
        )
        if isinstance(authority_assets, Mapping):
            seen = {str(value).removeprefix("sha256:") for value in input_manifest}
            for entry in authority_assets.values():
                if not isinstance(entry, Mapping):
                    continue
                candidate = next(
                    (
                        value
                        for value in (
                            entry.get("object_id"),
                            entry.get("media_id"),
                            entry.get("content_sha256"),
                            entry.get("digest"),
                            entry.get("sha256"),
                            entry.get("hash"),
                        )
                        if isinstance(value, str)
                        and len(value.removeprefix("sha256:")) == 64
                        and all(
                            ch in "0123456789abcdef"
                            for ch in value.removeprefix("sha256:")
                        )
                    ),
                    None,
                )
                if candidate is None:
                    continue
                normalized = candidate.removeprefix("sha256:")
                if normalized in seen:
                    continue
                seen.add(normalized)
                input_manifest.append(candidate)

    if (str(capability.id) == "rendering.timeline_visualize"
            and isinstance(idempotency_context, Mapping)
            and idempotency_context.get("mode") == "filmstrip"):
        # This authority is minted by managed-render preflight, never by a
        # public file argument. Generic-host materialization requires the
        # same immutable object in both inputs and the authorization manifest.
        video_id = idempotency_context.get("video_object_id")
        video_input = request_inputs.get("rendered_video")
        if (not isinstance(video_id, str) or not video_id.startswith("sha256:")
                or len(video_id) != 71
                or any(c not in "0123456789abcdef" for c in video_id[7:])
                or not isinstance(video_input, Mapping)
                or video_input.get("digest") != video_id
                or video_input.get("object_id") != video_id):
            raise CapabilityValidationError("filmstrip video admission identity mismatch")
        input_manifest.append(video_id)

    if (str(capability.id) == "rendering.timeline_visualize"
            and isinstance(idempotency_context, Mapping) and idempotency_context.get("mode") == "revision_diff"):
        compiled = idempotency_context["revision_diff"]
        for side in ("before", "after"):
            for asset in compiled[side]["registry"].get("assets", {}).values():
                if not isinstance(asset, Mapping):
                    continue
                digest = next((asset.get(key) for key in ("object_id", "media_id", "content_sha256", "digest")
                               if isinstance(asset.get(key), str) and re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", asset[key])), None)
                if digest is None:
                    raise CapabilityValidationError("revision diff media lacks immutable managed digest authority")
                normalized = "sha256:" + digest.removeprefix("sha256:")
                if normalized not in input_manifest:
                    input_manifest.append(normalized)
    if str(capability.id) == "rendering.timeline_visualize":
        authority_input = request_inputs.get("filmstrip_authority")
        if isinstance(authority_input, Mapping):
            authority_digest = authority_input.get("digest")
            if not isinstance(authority_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", authority_digest):
                raise CapabilityValidationError("filmstrip authority object must be a sha256 managed digest")
            input_manifest.append(authority_digest)
        transcript_input = request_inputs.get("transcript.json")
        if transcript_input is not None:
            if (
                not isinstance(transcript_input, Mapping)
                or not isinstance(transcript_input.get("digest"), str)
                or not isinstance(transcript_input.get("object_id"), str)
                or transcript_input["digest"] != transcript_input["object_id"]
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", transcript_input["digest"])
            ):
                raise CapabilityValidationError(
                    "transcript_file admission identity must be a matching sha256 digest/object_id"
                )
            input_manifest.append(transcript_input["digest"])

    # A filmstrip authority snapshot can mention the rendered video both as
    # the explicit ``rendered_video`` input and as a registry entry.  The
    # execution-request contract is an ordered *set* of CAS identities, so
    # collapse repeated witnesses before admission while retaining the first
    # occurrence's order.  Without this, an otherwise valid render->visualize
    # handoff is rejected as ``input_object_ids contains duplicate object IDs``.
    if input_manifest:
        unique_manifest: list[str] = []
        seen_manifest: set[str] = set()
        for object_id in input_manifest:
            normalized_id = str(object_id)
            if normalized_id in seen_manifest:
                continue
            seen_manifest.add(normalized_id)
            unique_manifest.append(normalized_id)
        input_manifest = unique_manifest

    try:
        input_manifest = merge_execution_input_manifest(
            execution_request,
            input_manifest,
        )
    except ExecutionRequestError as exc:
        raise CapabilityValidationError(str(exc)) from exc

    if _client is None:
        raise CapabilityInvocationError(
            "explicit generated runtime client is required for task admission"
        )

    tasks = getattr(_client, "tasks", None)
    create_task = getattr(tasks, "create", None)
    if not callable(create_task):
        raise CapabilityInvocationError(
            "runtime client does not expose generated task admission"
        )
    admission = {
        "project_id": project,
        "capability": str(capability.id),
        "spec": spec,
        "input_manifest": input_manifest,
        # RemoteTasks owns the completed admission (including the selected
        # capability digest and normalized execution request). Derive the key
        # there so every field Runtime compares participates in replay identity.
        "deterministic_idempotency": True,
        "storage_estimate": dict(storage_estimate) if storage_estimate is not None else None,
    }
    if generation_intent is not None:
        admission["generation_intent"] = generation_intent
        admission["settlement_effect"] = _generation_publish_effect(
            capability,
            project=project,
            generation_intent=generation_intent,
            variant_context=variant_context,
        )
    if execution_request is not None:
        admission["execution_request"] = dict(execution_request)
    if child_delegation is not None:
        from ._child_bridge import _validate_child_policy

        admission["child_delegation"] = _validate_child_policy(child_delegation)
    def admit(request: Mapping[str, Any], *, replace_frozen: bool = False) -> Any:
        request = dict(request)
        if _recovery is None:
            return create_task(**request)
        prepare = getattr(tasks, "prepare_admission", None)
        dispatch = getattr(tasks, "dispatch_admission", None)
        if not callable(prepare) or not callable(dispatch):
            raise CapabilityInvocationError("recovery requires separable Runtime task admission")
        if project:
            resolved = tasks._typed("get_project", project)
            if not resolved.ok:
                raise CapabilityInvocationError("recovery cannot resolve the canonical task project")
            canonical_project = resolved.data.get("project_id")
            if not isinstance(canonical_project, str) or not canonical_project:
                raise CapabilityInvocationError("recovery project lookup returned no canonical identity")
            request["project_id"] = canonical_project
            if isinstance(request.get("settlement_effect"), Mapping):
                effect = dict(request["settlement_effect"])
                if effect.get("target_id") == project:
                    effect["target_id"] = canonical_project
                request["settlement_effect"] = effect
        prepared = prepare(**request)
        if not prepared.ok:
            return prepared
        if replace_frozen:
            _recovery.freeze(prepared.data, metadata=_recovery_metadata or {}, replace=True)
        else:
            _recovery.freeze(prepared.data, metadata=_recovery_metadata or {})
        return dispatch(prepared.data)

    submitted_at = datetime.now(timezone.utc)
    data, rejection = _admission_resource(admit(admission))
    if rejection is not None:
        return "", "", "", None, {"ok": False, "error": rejection}, False, None
    replay = _admission_replay(data, submitted_at)
    if replay is not None and _task_state(data) in _FAILED_TASK_STATES and rerun:
        # The deterministic key returned the earlier failed task. A rerun
        # admits a fresh task under a new key; the failed one stays as history.
        # (With a recovery receipt the new admission replaces the frozen one.)
        replay = None
        admission = {**admission, "deterministic_idempotency": False, "idempotency_key": uuid.uuid4().hex}
        data, rejection = _admission_resource(admit(admission, replace_frozen=True))
        if rejection is not None:
            return "", "", "", None, {"ok": False, "error": rejection}, False, None

    run_id = str(data.get("run_id") or "")
    task_id = str(data.get("task_id") or "")
    if not run_id or not task_id:
        raise CapabilityInvocationError(
            "runtime task admission returned an incomplete task resource"
        )
    if _recovery is not None:
        _recovery.locate(data)
    attempt_id = str(data.get("attempt_id") or "")
    if replay is not None and _task_state(data) in _FAILED_TASK_STATES:
        # Refuse silently re-serving a failure: say what was replayed and how to run it again.
        error = {
            "code": "replayed_failed_task",
            "message": (
                f"task {task_id} already {_task_state(data)} with this exact input "
                f"(created {replay['created_at']}); it was replayed, not re-run. "
                "Pass rerun=True (CLI: --rerun) to admit a fresh task."
            ),
            "details": {"task_id": task_id, "run_id": run_id, "replayed": replay},
        }
        return run_id, task_id, attempt_id, None, {
            "ok": False,
            "run_id": run_id,
            "kernel_run_id": run_id,
            "kernel_task_id": task_id,
            "kernel_attempt_id": attempt_id,
            "task": dict(data),
            "replayed": replay,
            "error": error,
        }, False, None
    raw_result = {
        "ok": True,
        "run_id": run_id,
        "kernel_run_id": run_id,
        "kernel_task_id": task_id,
        "kernel_attempt_id": attempt_id,
        "task": dict(data),
    }
    if replay is not None:
        raw_result["replayed"] = replay
    return run_id, task_id, attempt_id, None, raw_result, True, None


_FAILED_TASK_STATES = frozenset({"failed", "cancelled"})
# Runtime created_at is the commit time of the admission. A fresh admission is
# stamped after our submit time; allow a little clock skew before calling it old.
_REPLAY_CLOCK_SKEW = timedelta(seconds=2)


def _task_state(data: Mapping[str, Any]) -> str:
    return str(data.get("state") or data.get("status") or "").lower()


def _admission_resource(
    result: Any,
) -> tuple[Mapping[str, Any] | None, dict[str, Any] | None]:
    """Return the admitted task resource or the runtime rejection."""
    result_ok = bool(getattr(result, "ok", isinstance(result, Mapping)))
    data = getattr(result, "data", result if isinstance(result, Mapping) else None)
    if not result_ok:
        error = getattr(result, "error", None)
        if hasattr(error, "as_dict"):
            error = error.as_dict()
        elif isinstance(error, Mapping):
            error = dict(error)
        else:
            error = {
                "code": "runtime_error",
                "message": "runtime rejected task admission",
                "details": {},
            }
        return None, error
    if not isinstance(data, Mapping):
        raise CapabilityInvocationError("runtime task admission returned no task resource")
    return data, None


def _admission_replay(
    data: Mapping[str, Any],
    submitted_at: datetime,
) -> dict[str, Any] | None:
    """Describe the admitted task when the Runtime returned an earlier task (idempotent replay).

    A fresh admission is created at or after ``submitted_at``; a task created
    before the submit (beyond clock skew) can only have come from the replayed key.
    """
    raw_created = data.get("created_at")
    if not isinstance(raw_created, str) or not raw_created:
        return None
    try:
        created = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if created >= submitted_at - _REPLAY_CLOCK_SKEW:
        return None
    return {
        "task_id": str(data.get("task_id") or ""),
        "run_id": str(data.get("run_id") or ""),
        "state": _task_state(data),
        "created_at": raw_created,
    }


def _read_task_managed_outputs(
    client: Any,
    task_id: str,
) -> tuple[list[Any] | None, dict[str, Any] | None]:
    """Read the Runtime-owned managed-output page for one completed task."""
    tasks = getattr(client, "tasks", None)
    reader = getattr(tasks, "list_managed_outputs", None)
    if not callable(reader):
        return None, {
            "code": "managed_output_readback_unavailable",
            "message": "runtime client does not expose task managed-output readback",
            "details": {"task_id": task_id},
        }
    try:
        value = reader(task_id)
    except Exception as exc:
        return None, {
            "code": "managed_output_readback_unavailable",
            "message": "managed outputs could not be read for the completed task",
            "details": {"task_id": task_id, "error_type": type(exc).__name__},
        }
    if hasattr(value, "ok") and hasattr(value, "data"):
        if not bool(value.ok):
            error = getattr(value, "error", None)
            if hasattr(error, "as_dict"):
                error = error.as_dict()
            return None, {
                "code": "managed_output_readback_unavailable",
                "message": "managed outputs could not be read for the completed task",
                "details": _json_safe(error) if error is not None else {"task_id": task_id},
            }
        value = value.data
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], list):
        return list(value[0]), None
    if isinstance(value, list):
        return value, None
    return None, {
        "code": "managed_output_readback_invalid",
        "message": "managed-output readback returned an invalid page",
        "details": {"task_id": task_id},
    }


_MANAGED_OUTPUT_SETTLE_SECONDS = 10.0
_MANAGED_OUTPUT_SETTLE_POLL_SECONDS = 0.5


def _settle_task_managed_outputs(
    client: Any,
    task_id: str,
    *,
    expects_outputs: bool,
    timeout_seconds: float = _MANAGED_OUTPUT_SETTLE_SECONDS,
    poll_seconds: float = _MANAGED_OUTPUT_SETTLE_POLL_SECONDS,
) -> tuple[list[Any] | None, dict[str, Any] | None]:
    """Read a terminal task's managed outputs, polling briefly while they settle.

    A capability that declares outputs must not finish with an empty listing:
    rows may land a moment after the task turns terminal, so poll (bounded) and
    then fail naming the task instead of returning ok with nothing.
    """
    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        rows, read_error = _read_task_managed_outputs(client, task_id)
        if read_error is not None:
            return None, read_error
        if rows or not expects_outputs:
            return rows or [], None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, {
                "code": "managed_outputs_unsettled",
                "message": (
                    f"task {task_id} is terminal but listed no managed outputs after "
                    f"{float(timeout_seconds):g} s; inspect it with "
                    f"python3 -m astrid tasks show {task_id}"
                ),
                "details": {"task_id": task_id, "timeout_seconds": float(timeout_seconds)},
            }
        time.sleep(min(float(poll_seconds), remaining))


def _capability_declares_outputs(capability: Any) -> bool:
    declared = getattr(capability, "outputs", None)
    if not declared:
        definition = getattr(capability, "definition", None)
        declared = (
            definition.get("outputs", ())
            if isinstance(definition, Mapping)
            else getattr(definition, "outputs", ())
        )
    return bool(declared)


def _wait_for_kernel_task(
    client: Any,
    *,
    task_id: str,
    run_id: str,
    timeout_seconds: float,
    poll_seconds: float,
    read_managed_outputs: bool = False,
    expects_outputs: bool = False,
) -> tuple[dict[str, Any], bool, str]:
    """Follow one admitted task until completion, failure, or requested cancellation."""
    if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
        raise CapabilityValidationError("wait timeout_seconds must be positive")
    if not math.isfinite(float(poll_seconds)) or poll_seconds <= 0:
        raise CapabilityValidationError("wait poll_seconds must be positive")
    tasks = getattr(client, "tasks", None)
    show = getattr(tasks, "show", None)
    if not callable(show):
        raise CapabilityInvocationError(
            "runtime client does not expose task status for synchronous invocation"
        )

    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        try:
            observed = show(task_id)
        except Exception as exc:
            return {
                "ok": False,
                "run_id": run_id,
                "kernel_run_id": run_id,
                "kernel_task_id": task_id,
                "error": {
                    "code": "task_status_unavailable",
                    "message": "render task status could not be read",
                    "details": {"error_type": type(exc).__name__},
                },
            }, False, ""
        observed_ok = bool(getattr(observed, "ok", isinstance(observed, Mapping)))
        data = getattr(observed, "data", observed if isinstance(observed, Mapping) else None)
        if not observed_ok or not isinstance(data, Mapping):
            error = getattr(observed, "error", None)
            if hasattr(error, "as_dict"):
                error = error.as_dict()
            return {
                "ok": False,
                "run_id": run_id,
                "kernel_run_id": run_id,
                "kernel_task_id": task_id,
                "error": {
                    "code": "task_status_unavailable",
                    "message": "render task status could not be read",
                    "details": dict(error) if isinstance(error, Mapping) else {},
                },
            }, False, ""

        task = dict(data)
        state = str(task.get("state") or task.get("status") or "").lower()
        attempt_id = str(task.get("attempt_id") or "")
        if state in {"succeeded", "completed"}:
            settled = task.get("result")
            settled = dict(settled) if isinstance(settled, Mapping) else {}
            output_rows = settled.get("outputs")
            completed = {
                "ok": True,
                "run_id": run_id,
                "kernel_run_id": run_id,
                "kernel_task_id": task_id,
                "kernel_attempt_id": attempt_id,
                "state": "completed",
                "task": task,
                "result": settled,
                "outputs": {
                    "artifacts": list(output_rows)
                    if isinstance(output_rows, list)
                    else []
                },
            }
            if read_managed_outputs:
                managed_outputs, read_error = _settle_task_managed_outputs(
                    client, task_id, expects_outputs=expects_outputs,
                )
                if read_error is not None:
                    if read_error.get("code") == "managed_outputs_unsettled":
                        raise CapabilityInvocationError(
                            str(read_error["message"]),
                            details=dict(read_error["details"]),
                        )
                    completed["error"] = read_error
                    completed["ok"] = False
                    return completed, False, attempt_id
                completed["managed_outputs"] = managed_outputs or []
            return completed, True, attempt_id
        if state in {"failed", "cancelled", "cancel_requested"}:
            settled = task.get("result")
            settled = dict(settled) if isinstance(settled, Mapping) else {}
            failure = settled.get("error")
            if isinstance(failure, Mapping):
                message = str(failure.get("message") or f"render task {state}")
            else:
                message = str(failure or f"render task {state}")
            return {
                "ok": False,
                "run_id": run_id,
                "kernel_run_id": run_id,
                "kernel_task_id": task_id,
                "kernel_attempt_id": attempt_id,
                "state": state,
                "task": task,
                "error": {
                    "code": f"task_{state}",
                    "message": message,
                    "details": {"state": state, "result": settled},
                },
            }, False, attempt_id

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "ok": False,
                "run_id": run_id,
                "kernel_run_id": run_id,
                "kernel_task_id": task_id,
                "kernel_attempt_id": attempt_id,
                "state": state or "unknown",
                "task": task,
                "error": {
                    "code": "task_wait_timeout",
                    "message": "render task did not reach a terminal state before the wait timeout",
                    "details": {
                        "state": state or "unknown",
                        "timeout_seconds": float(timeout_seconds),
                    },
                },
            }, False, attempt_id
        time.sleep(min(float(poll_seconds), remaining))


def _observe_task_invocation(
    client: Any,
    *,
    capability_id: str,
    capability_type: str,
    native_kind: str,
    task_id: str,
    run_id: str,
    initial_raw_result: Mapping[str, Any] | None = None,
    initial_task: Mapping[str, Any] | None = None,
    initial_error: Mapping[str, Any] | None = None,
    initial_ok: bool = True,
    attempt_id: str = "",
    wait: bool = False,
    timeout_seconds: float = 3600.0,
    poll_seconds: float = 1.0,
    read_managed_outputs: bool = False,
    discover_managed_outputs: bool = True,
    executor_version: str | None = None,
    out: Path | str | None = None,
    project: str | None = None,
    authority_context: Mapping[str, Any] | None = None,
    manifest_path: Path | str | None = None,
    expects_outputs: bool = False,
    replay_info: Mapping[str, Any] | None = None,
) -> InvocationResult:
    """Observe a known Runtime task and build the existing SDK result shape.

    ``initial_raw_result`` can be the admission response or a receipt/legacy
    locator's already-read task. When a legacy locator has no saved DTO, a
    non-waiting observation performs one authorized task read. Waiting always
    uses the same Runtime polling and result construction as fresh invocation.
    This helper reads task/output state only; it never dispatches or settles.
    """
    raw_result = dict(initial_raw_result) if isinstance(initial_raw_result, Mapping) else None
    ok = bool(initial_ok)
    if raw_result is None and isinstance(initial_task, Mapping):
        attempt_id = str(initial_task.get("attempt_id") or attempt_id)
        raw_result = {
            "ok": True,
            "run_id": run_id,
            "kernel_run_id": run_id,
            "kernel_task_id": task_id,
            "kernel_attempt_id": attempt_id,
            "task": dict(initial_task),
        }
    elif raw_result is None and isinstance(initial_error, Mapping):
        raw_result = {"ok": False, "error": dict(initial_error)}
        ok = False
    if raw_result is None and ok and not wait:
        tasks = getattr(client, "tasks", None)
        show = getattr(tasks, "show", None)
        if not callable(show):
            raise CapabilityInvocationError(
                "runtime client does not expose task status for invocation observation"
            )
        try:
            observed = show(task_id)
        except Exception as exc:
            raw_result = {
                "ok": False,
                "run_id": run_id,
                "kernel_run_id": run_id,
                "kernel_task_id": task_id,
                "error": {
                    "code": "task_status_unavailable",
                    "message": "render task status could not be read",
                    "details": {"error_type": type(exc).__name__},
                },
            }
            ok = False
            observed = None
        observed_ok = bool(getattr(observed, "ok", isinstance(observed, Mapping)))
        data = getattr(observed, "data", observed if isinstance(observed, Mapping) else None)
        if observed is None:
            pass
        elif observed_ok and isinstance(data, Mapping):
            raw_result = {
                "ok": True,
                "run_id": run_id,
                "kernel_run_id": run_id,
                "kernel_task_id": task_id,
                "kernel_attempt_id": str(data.get("attempt_id") or ""),
                "task": dict(data),
            }
            attempt_id = str(data.get("attempt_id") or attempt_id)
        else:
            error = getattr(observed, "error", None)
            if hasattr(error, "as_dict"):
                error = error.as_dict()
            raw_result = {
                "ok": False,
                "run_id": run_id,
                "kernel_run_id": run_id,
                "kernel_task_id": task_id,
                "error": {
                    "code": "task_status_unavailable",
                    "message": "render task status could not be read",
                    "details": dict(error) if isinstance(error, Mapping) else {},
                },
            }
            ok = False
    elif raw_result is None:
        raw_result = {"ok": False}

    if wait and ok:
        raw_result, ok, waited_attempt_id = _wait_for_kernel_task(
            client,
            task_id=task_id,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            read_managed_outputs=read_managed_outputs,
            expects_outputs=expects_outputs,
        )
        if waited_attempt_id:
            attempt_id = waited_attempt_id
        if replay_info is not None:
            raw_result = {**dict(raw_result), "replayed": dict(replay_info)}

    raw_result = dict(raw_result)
    if not wait:
        observed_task = raw_result.get("task")
        if isinstance(observed_task, Mapping):
            state = str(observed_task.get("state") or observed_task.get("status") or "").lower()
            if state:
                raw_result.setdefault(
                    "state",
                    "completed" if state in {"succeeded", "completed"} else state,
                )
            settled = observed_task.get("result")
            if state in {"succeeded", "completed"}:
                settled = dict(settled) if isinstance(settled, Mapping) else {}
                raw_result.setdefault("result", settled)
                output_rows = settled.get("outputs")
                raw_result.setdefault("outputs", {
                    "artifacts": list(output_rows) if isinstance(output_rows, list) else []
                })
            elif isinstance(settled, Mapping):
                raw_result.setdefault("result", dict(settled))
    observed_task = raw_result.get("task")
    observed_state = str(observed_task.get("state") or observed_task.get("status") or "").lower() if isinstance(observed_task, Mapping) else ""
    if isinstance(observed_task, Mapping) and any(
        observed_task.get(field) is not None and observed_task.get(field) != expected
        for field, expected in (("task_id", task_id), ("run_id", run_id), ("capability_id", capability_id))
    ):
        ok = False
        raw_result["ok"] = False
        raw_result["error"] = {"code": "task_identity_mismatch", "message": "Runtime task identity disagrees with the supplied locator", "details": {"expected_task_id": task_id, "expected_run_id": run_id}}

    settled_success = ok and (wait or (isinstance(authority_context, Mapping) and
                                      observed_state in {"succeeded", "completed"}))
    reference_links = authority_context.get("reference_links") if isinstance(authority_context, Mapping) else None
    if (settled_success and capability_type == "executor" and "managed_outputs" not in raw_result
            and (discover_managed_outputs or read_managed_outputs or reference_links)):
        # Read rows through the shared observer so fresh and resumed tasks
        # expose the same handles. Explicit readback and frozen reference
        # links require success; legacy callers retain optional discovery.
        rows, read_error = _read_task_managed_outputs(client, task_id)
        if read_error is None:
            raw_result["managed_outputs"] = rows or []
        elif read_managed_outputs or reference_links:
            raw_result["ok"] = False
            raw_result["error"] = read_error
            ok = False
    if ok and settled_success and isinstance(raw_result.get("managed_outputs"), list):
        # Each row carries its handle and, for result media, a viewable local
        # copy-on-write view (deterministic per digest).
        from .media_handles import annotate_output_rows

        raw_result["managed_outputs"] = annotate_output_rows(client, project, raw_result["managed_outputs"])
    if settled_success and ok and reference_links:
        from .media_handles import link_outputs_to_references

        raw_result["reference_links"] = link_outputs_to_references(
            client, str(project), reference_links, task_id=task_id, run_id=run_id,
            output_rows=[row for row in raw_result.get("managed_outputs") or [] if isinstance(row, Mapping)],
        )
    if executor_version is not None:
        raw_result.setdefault("executor_version", executor_version)
    raw_result.setdefault("kernel_run_id", run_id)
    raw_result.setdefault("kernel_task_id", task_id)
    raw_result.setdefault("kernel_attempt_id", attempt_id)
    discovered_manifest = (
        str(manifest_path)
        if manifest_path
        else _discover_invocation_manifest_path(raw_result, out=out)
    )
    if (
        wait
        and ok
        and capability_id == "rendering.timeline_visualize"
        and isinstance(authority_context, Mapping)
        and authority_context.get("mode") in {"filmstrip", "input_only", "composed_capture"}
    ):
        discovered_manifest = _materialize_filmstrip_outputs(
            raw_result,
            client,
            project=project,
        )
        _cap_filmstrip_cache(raw_result, project=project)

    error = raw_result.get("error")
    sdk_error = (
        {
            **dict(error),
            "sdk_error": "CapabilityRuntimeError",
            "sdk_category": "runtime",
        }
        if isinstance(error, Mapping)
        else None
    )
    run_id_raw = raw_result.get("run_id")
    run_root_raw = raw_result.get("run_root")
    executor_version_raw = raw_result.get("executor_version")
    return InvocationResult(
        capability_id=capability_id,
        capability_type=capability_type,
        native_kind=native_kind,
        ok=ok,
        error=sdk_error,
        manifest_path=discovered_manifest,
        raw_result=raw_result,
        run_id=run_id_raw if isinstance(run_id_raw, str) and run_id_raw else run_id,
        run_root=(
            str(Path(run_root_raw).expanduser().resolve())
            if isinstance(run_root_raw, str) and run_root_raw
            else None
        ),
        outputs=_invocation_outputs(
            raw_result,
            manifest_path=discovered_manifest,
            capability_id=capability_id,
        ),
        executor_version=(
            executor_version_raw
            if isinstance(executor_version_raw, str) and executor_version_raw
            else None
        ),
        kernel_run_id=run_id,
        kernel_task_id=task_id,
        kernel_attempt_id=attempt_id,
        replayed=replay_info is not None,
        replayed_task_id=str(replay_info["task_id"]) if replay_info is not None else None,
        replayed_from=str(replay_info["created_at"]) if replay_info is not None else None,
    )


def observe_task_invocation(
    client: Any,
    *,
    capability_id: str,
    capability_type: str,
    native_kind: str,
    task_id: str,
    run_id: str,
    attempt_id: str | None = None,
    wait: bool = True,
    timeout_seconds: float = 3600.0,
    poll_seconds: float = 1.0,
    read_managed_outputs: bool = False,
    executor_version: str | None = None,
    manifest_path: str | None = None,
) -> InvocationResult:
    """Read one caller-supplied Runtime locator through the SDK result path.

    This is a read-only compatibility seam for callers that already possess a
    task/run locator. It never admits, retries, or settles a task; a locator is
    not evidence that an admission happened or that the observed task matches.
    Runtime's response must match the supplied identity before success is
    returned.

    With ``wait=False``, ``ok`` means the task read succeeded; inspect the
    observed task state before treating the underlying work as complete.
    """
    required = {
        "capability_id": capability_id,
        "capability_type": capability_type,
        "native_kind": native_kind,
        "task_id": task_id,
        "run_id": run_id,
    }
    if not all(isinstance(value, str) and value for value in required.values()):
        raise CapabilityValidationError(
            "task observation requires non-empty capability and task/run identities"
        )
    if attempt_id is not None and (not isinstance(attempt_id, str) or not attempt_id):
        raise CapabilityValidationError("attempt_id must be a non-empty string when supplied")
    if not isinstance(wait, bool) or not isinstance(read_managed_outputs, bool):
        raise CapabilityValidationError("wait and read_managed_outputs must be booleans")
    observed = _observe_task_invocation(
        client,
        capability_id=capability_id,
        capability_type=capability_type,
        native_kind=native_kind,
        task_id=task_id,
        run_id=run_id,
        attempt_id=attempt_id or "",
        wait=wait,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        read_managed_outputs=read_managed_outputs,
        executor_version=executor_version,
        manifest_path=manifest_path,
    )
    if not observed.ok:
        return observed
    task = observed.raw_result.get("task") if isinstance(observed.raw_result, Mapping) else None
    observed_task_id = (
        task.get("task_id", task.get("id")) if isinstance(task, Mapping) else None
    )
    observed_run_id = task.get("run_id") if isinstance(task, Mapping) else None
    observed_attempt_id = task.get("attempt_id") if isinstance(task, Mapping) else None
    observed_capability_id = task.get("capability_id") if isinstance(task, Mapping) else None
    if (
        observed_task_id != task_id
        or observed_run_id != run_id
        or (attempt_id is not None and observed_attempt_id != attempt_id)
        or (
            isinstance(observed_capability_id, str)
            and observed_capability_id != capability_id
        )
    ):
        error = {
            "code": "task_identity_mismatch",
            "message": "Runtime task identity disagrees with the supplied locator",
            "details": {
                "expected_task_id": task_id,
                "expected_run_id": run_id,
                "expected_attempt_id": attempt_id,
                "expected_capability_id": capability_id,
            },
        }
        raw_result = dict(observed.raw_result)
        raw_result["ok"] = False
        raw_result["error"] = error
        return InvocationResult(
            capability_id=observed.capability_id,
            capability_type=observed.capability_type,
            native_kind=observed.native_kind,
            ok=False,
            error=error,
            manifest_path=observed.manifest_path,
            raw_result=raw_result,
            run_id=run_id,
            run_root=observed.run_root,
            outputs=observed.outputs,
            executor_version=observed.executor_version,
            kernel_run_id=run_id,
            kernel_task_id=task_id,
            kernel_attempt_id=attempt_id or "",
        )
    if not wait and read_managed_outputs:
        state = str(task.get("state") or task.get("status") or "").lower()
        if state in {"succeeded", "completed"}:
            managed_outputs, read_error = _read_task_managed_outputs(client, task_id)
            if read_error is not None:
                raw_result = dict(observed.raw_result)
                raw_result["ok"] = False
                raw_result["error"] = read_error
                return InvocationResult(
                    capability_id=observed.capability_id,
                    capability_type=observed.capability_type,
                    native_kind=observed.native_kind,
                    ok=False,
                    error=read_error,
                    manifest_path=observed.manifest_path,
                    raw_result=raw_result,
                    run_id=observed.run_id,
                    run_root=observed.run_root,
                    outputs=observed.outputs,
                    executor_version=observed.executor_version,
                    kernel_run_id=run_id,
                    kernel_task_id=task_id,
                    kernel_attempt_id=attempt_id or "",
                )
            raw_result = dict(observed.raw_result)
            raw_result["managed_outputs"] = managed_outputs or []
            observed = InvocationResult(
                capability_id=observed.capability_id,
                capability_type=observed.capability_type,
                native_kind=observed.native_kind,
                ok=True,
                error=observed.error,
                manifest_path=observed.manifest_path,
                raw_result=raw_result,
                run_id=observed.run_id,
                run_root=observed.run_root,
                outputs=_invocation_outputs(
                    raw_result,
                    manifest_path=observed.manifest_path,
                    capability_id=capability_id,
                ),
                executor_version=observed.executor_version,
                kernel_run_id=run_id,
                kernel_task_id=task_id,
                kernel_attempt_id=observed.kernel_attempt_id or attempt_id or "",
            )
    return observed


def _resume_receipt_invocation(receipt, *, client, wait, timeout_seconds, poll_seconds):
    """Observe a frozen admission before any live manifest/preflight resolution."""
    result = receipt.recover_task()
    metadata = receipt.data["metadata"]
    locator = receipt.data["locator"] or {}
    run_id, task_id = locator.get("run_id", ""), locator.get("task_id", "")
    attempt_id = ""
    if result.ok:
        task = result.data if isinstance(result.data, Mapping) else None
        raw_error = None
    else:
        task = None
        raw_error = result.error.as_dict()
    return _observe_task_invocation(
        client,
        capability_id=metadata["capability_id"],
        capability_type=metadata["capability_type"],
        native_kind=metadata["native_kind"],
        task_id=task_id,
        run_id=run_id,
        initial_task=task,
        initial_error=raw_error,
        initial_ok=bool(result.ok),
        attempt_id=attempt_id,
        wait=wait,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        read_managed_outputs=bool(metadata.get("read_managed_outputs")),
        discover_managed_outputs=bool(metadata.get("discover_managed_outputs", False)),
        executor_version=metadata.get("executor_version"),
        out=metadata.get("out"),
        project=metadata.get("project"),
        authority_context=metadata.get("authority_context"),
    )


def _invoke_bridge_child(
    capability: Any, *, registries: Any, inputs: dict[str, Any], bridge: Any,
    child_key: str | None, wait: bool, timeout_seconds: float, poll_seconds: float,
) -> InvocationResult:
    """Use only attempt-scoped admission/readback, preserving SDK result DTOs."""
    from types import SimpleNamespace
    from astrid.core.foundation.hash import executor_definition_digest
    from ._child_bridge import (
        _BridgeRejected, _ChildOutputBinding, _child_request, _valid_id, _validate_output_page,
    )

    digest = executor_definition_digest(registries[0].get(capability.id))
    request = _child_request(capability, inputs, digest="sha256:" + digest, child_key=child_key,
                             wait=wait, timeout_seconds=timeout_seconds, poll_seconds=poll_seconds)
    try:
        data = bridge.submit(request)
    except _BridgeRejected as exc:
        error = {**exc.error, "sdk_error": "CapabilityRuntimeError", "sdk_category": "runtime"}
        return InvocationResult(capability_id=capability.id, capability_type=capability.capability_type,
                                native_kind=capability.native_kind, ok=False, error=error,
                                raw_result={"ok": False, "error": exc.error}, executor_version=digest)
    if (not isinstance(data, Mapping) or not _valid_id(data.get("task_id"))
            or not _valid_id(data.get("run_id"))):
        bridge.close()
        raise CapabilityInvocationError("child bridge admission returned an incomplete task resource")
    run_id, task_id = data["run_id"], data["task_id"]
    attempt_id = str(data.get("attempt_id") or "")
    raw = {"ok": True, "run_id": run_id, "kernel_run_id": run_id,
           "kernel_task_id": task_id, "kernel_attempt_id": attempt_id, "task": dict(data)}
    ok = True
    if wait:
        deadline = time.monotonic() + float(timeout_seconds)
        latest_status: Mapping[str, Any] = data
        protocol_error: CapabilityInvocationError | None = None

        def status(observed_id: str) -> Any:
            nonlocal latest_status, protocol_error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest_status
            try:
                observed = bridge.status(request["child_key"], observed_id,
                                         timeout_seconds=remaining)
            except _BridgeRejected as exc:
                return SimpleNamespace(ok=False, data=None, error=exc.error)
            if not isinstance(observed, Mapping) or observed.get("task_id") != task_id or observed.get("run_id") != run_id:
                bridge.close()
                protocol_error = CapabilityInvocationError("child bridge status returned a foreign task resource")
                raise protocol_error
            latest_status = observed
            return observed

        def read_outputs(observed_id: str) -> Any:
            try:
                rows = bridge.outputs(request["child_key"], observed_id,
                                      timeout_seconds=max(0.000001, deadline - time.monotonic()))
                if not _valid_id(latest_status.get("attempt_id")):
                    raise ValueError("invalid settled child attempt")
                return _validate_output_page(rows, task_id=task_id, run_id=run_id,
                                             attempt_id=latest_status["attempt_id"])
            except _BridgeRejected as exc:
                return SimpleNamespace(ok=False, data=None, error=exc.error)
            except ValueError as exc:
                bridge.close()
                raise CapabilityInvocationError("child bridge returned foreign or invalid outputs") from exc

        read_client = SimpleNamespace(tasks=SimpleNamespace(show=status, list_managed_outputs=read_outputs))
        raw, ok, waited_attempt = _wait_for_kernel_task(
            read_client, task_id=task_id, run_id=run_id, timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds, read_managed_outputs=True,
        )
        if protocol_error is not None:
            raise protocol_error
        if waited_attempt:
            attempt_id = waited_attempt
        settled = raw.get("result", {})
        payload = settled.get("payload", {}) if isinstance(settled, Mapping) else {}
        if ok and isinstance(payload, Mapping) and "action_result" in payload:
            from .actions import validate_action_output_definition

            validate_action_output_definition(capability.definition, payload["action_result"])
    raw["executor_version"] = digest
    error = raw.get("error")
    result = InvocationResult(
        capability_id=capability.id, capability_type=capability.capability_type,
        native_kind=capability.native_kind, ok=ok,
        error={**error, "sdk_error": "CapabilityRuntimeError", "sdk_category": "runtime"}
        if isinstance(error, Mapping) else None,
        raw_result=raw, run_id=run_id, outputs=_invocation_outputs(
            raw, manifest_path=None, capability_id=capability.id,
        ), executor_version=digest, kernel_run_id=run_id,
        kernel_task_id=task_id, kernel_attempt_id=attempt_id,
    )
    if wait and ok:
        object.__setattr__(result, "_child_output_binding",
                           _ChildOutputBinding(result, bridge, request["child_key"]))
    return result


_AUDIO_TTS_PASSTHROUGH_INPUTS = frozenset({"voice", "rate", "volume", "pitch", "provider"})
_AUDIO_TTS_IGNORED_INPUTS = frozenset({"mode", "model", "execution"})


def _speech_inputs_from_audio_request(inputs: Mapping[str, Any] | None) -> dict[str, Any]:
    """Map generation.generate_audio mode=tts onto generation.generate_speech.

    ``text`` is the exact spoken words; ``prompt`` is accepted as an alias
    because audio callers use it for the main text input.  ``model`` and
    ``execution`` select a music backend and are ignored for speech.
    """
    request = dict(inputs or {})
    unsupported = sorted(set(request) - _AUDIO_TTS_PASSTHROUGH_INPUTS - _AUDIO_TTS_IGNORED_INPUTS - {"text", "prompt"})
    if unsupported:
        raise CapabilityValidationError(
            "generation.generate_audio mode=tts accepts text, voice, rate, volume, pitch and provider; "
            f"unsupported input(s): {', '.join(unsupported)}"
        )
    text = request.get("text")
    prompt = request.get("prompt")
    if text is not None and prompt is not None and text != prompt:
        raise CapabilityValidationError("mode=tts received conflicting text and prompt; pass only text")
    spoken = text if text is not None else prompt
    if not isinstance(spoken, str) or not spoken.strip():
        raise CapabilityValidationError("mode=tts requires non-empty text (the exact words to speak)")
    speech: dict[str, Any] = {"text": spoken}
    for key in _AUDIO_TTS_PASSTHROUGH_INPUTS:
        if key in request:
            speech[key] = request[key]
    return speech


def invoke(
    capability_id: str,
    *,
    kind: Any,
    project_root: str | Path | None = None,
    extra_pack_roots: tuple[str, ...] = (),
    banodoco_config: Any | None = None,
    active_theme: str | Path | None = None,
    include_missing_roots: bool = False,
    out: Path | str | None = None,
    project: str | None = None,
    inputs: Mapping[str, Any] | None = None,
    execution_request: ExecutionRequest | Mapping[str, Any] | None = None,
    idempotency_context: Mapping[str, Any] | None = None,
    child_delegation: Mapping[str, Any] | None = None,
    child_key: str | None = None,
    outputs: Mapping[str, Any] | None = None,
    brief: Path | str | None = None,
    dry_run: bool = False,
    check_binaries: bool = False,
    python_exec: str | None = None,
    verbose: bool = False,
    argv: tuple[str, ...] = (),
    orchestrator_args: tuple[str, ...] = (),
    registry: Any | None = None,
    client: Any | None = None,
    wait: bool = False,
    timeout_seconds: float = 3600.0,
    poll_seconds: float = 1.0,
    rerun: bool = False,
    _include_internal: bool = False,
    _internal_dispatch_token: object | None = None,
    recovery_path: Path | str | None = None,
    resume: bool = False,
    read_managed_outputs: bool = False,
    _recovery: Any | None = None,
) -> InvocationResult:
    """Invoke a selected capability through Runtime or the installed host bridge.

    ``child_delegation`` is parent admission policy, outside action inputs.
    Inside an admitted action, a private host bridge handles child calls.
    ``child_key`` identifies one logical child (and requires that bridge);
    omitting it uses a stable key derived from exact capability and inputs.
    """
    from . import _child_bridge

    child_mode = _child_bridge._bridge is not None or child_key is not None
    if child_mode and child_delegation is not None:
        raise CapabilityValidationError("child invocation cannot set child_delegation")
    validated_child_policy = _child_bridge._validate_child_policy(child_delegation)
    if child_mode and _child_bridge._bridge is None:
        raise CapabilityInvocationError("child invocation requires a live host bridge")
    if child_mode and any(value is not None for value in (
        execution_request, idempotency_context, outputs, project, out, brief, python_exec,
    )):
        raise CapabilityValidationError("child invocation accepts only declared inputs and child wait controls")
    if _include_internal and _internal_dispatch_token is not _INTERNAL_DISPATCH_TOKEN:
        raise TypeError("private capabilities can only be invoked through internal dispatch")
    if not isinstance(read_managed_outputs, bool):
        raise CapabilityValidationError("read_managed_outputs must be a boolean")
    if resume and recovery_path is None:
        raise CapabilityValidationError("resume requires recovery_path")
    if recovery_path is not None and _recovery is None:
        if dry_run:
            raise CapabilityValidationError("recovery is unavailable for dry-run previews")
        from .recovery import AdmissionReceipt

        forwarded = {name: value for name, value in locals().items()
                     if name in inspect.signature(invoke).parameters}
        forwarded.pop("forwarded", None)
        forwarded.pop("AdmissionReceipt", None)
        request = {name: forwarded[name] for name in (
            "capability_id", "kind", "project", "inputs", "outputs", "out", "brief",
            "execution_request", "idempotency_context", "project_root", "extra_pack_roots",
            "python_exec", "orchestrator_args", "argv", "read_managed_outputs", "child_delegation",
        )}
        try:
            with AdmissionReceipt(recovery_path, request=request, client=client, resume=resume) as receipt:
                if resume:
                    return _resume_receipt_invocation(receipt, client=client, wait=wait,
                        timeout_seconds=timeout_seconds, poll_seconds=poll_seconds)
                forwarded["_recovery"] = receipt
                forwarded.pop("capability_id")
                return invoke(capability_id, **forwarded)
        except OSError as exc:
            raise CapabilityInvocationError("recovery receipt storage is unavailable") from exc
    try:
        normalized_execution_request = normalize_execution_request(execution_request)
    except ExecutionRequestError as exc:
        raise CapabilityValidationError(str(exc)) from exc
    _client = client
    sdk_module = _sdk_module()
    include_elements = kind == "element"
    registries = sdk_module._load_registries(
        project_root=project_root,
        extra_pack_roots=extra_pack_roots,
        banodoco_config=banodoco_config,
        include_missing_roots=include_missing_roots,
        include_elements=include_elements,
        include_internal=_include_internal,
    )
    # Keep the public generation entrypoint while selecting a separately
    # bounded runtime profile for Codex (the cloud profile remains closed).
    if capability_id == "generation.generate_image" and (inputs or {}).get("execution") == "codex":
        capability_id = "generation.generate_image_codex"
    # generation.generate_audio mode=tts is the speech route: it is served by
    # the exact-text Edge TTS executor, not by the music backend.
    if capability_id == "generation.generate_audio" and (inputs or {}).get("mode") == "tts":
        capability_id = "generation.generate_speech"
        inputs = _speech_inputs_from_audio_request(inputs)
    capability = sdk_module.get_capability(
        capability_id,
        kind=kind,
        project_root=project_root,
        extra_pack_roots=extra_pack_roots,
        banodoco_config=banodoco_config,
        include_missing_roots=include_missing_roots,
        _registries=registries,
    )
    if capability.capability_type == "element":
        raise UnsupportedCapabilityError(f"elements are not invokable via the SDK: {capability.id}")

    # The unified action selector resolves a native route before admission.
    # Runtime task kinds and existing SDK result DTOs retain their contracts.
    if kind == "action":
        kind = capability.capability_type

    intent_modality = _generation_capability_modality(capability)
    request_inputs = dict(inputs or {})
    if capability.id == "rendering.render":
        # Apply caller/mode checks before delegated child dispatch as well as
        # top-level preflight. Child file custody still belongs to the bridge.
        _validate_render_input_mode(request_inputs)
    variant_context: dict[str, Any] | None = None
    variant_of_supplied = "variant_of" in request_inputs
    primary_supplied = "primary" in request_inputs
    variant_of = request_inputs.pop("variant_of", None)
    primary = request_inputs.pop("primary", "preserve")
    if (variant_of_supplied or primary_supplied) and intent_modality is None:
        raise CapabilityValidationError(
            "variant_of and primary are only accepted for generation capabilities"
        )
    _validate_variant_controls(
        variant_of if variant_of_supplied else None,
        primary,
        modality=intent_modality,
        primary_supplied=primary_supplied,
    )
    if variant_of is not None:
        # Project resolution below may use the Runtime's durable selection;
        # defer the read-only lineage lookup until that identity is known.
        variant_context = {"variant_of": variant_of, "primary": primary}
    generation_intent: dict[str, Any] | None = None
    if "generation_intent" in request_inputs:
        if intent_modality is None and capability.id != "vibecomfy.run":
            raise CapabilityValidationError(
                "generation_intent is only accepted for generation capabilities"
            )
        if intent_modality is None:
            raw_intent = request_inputs["generation_intent"]
            intent_modality = (
                raw_intent.get("modality")
                if isinstance(raw_intent, Mapping)
                else None
            )
            if intent_modality not in {"image", "video", "audio"}:
                raise CapabilityValidationError(
                    "vibecomfy.run generation_intent must declare image, video, or audio modality"
                )
        generation_intent = _validate_generation_intent(
            request_inputs["generation_intent"],
            modality=intent_modality,
        )
        # Intent is admission metadata, not an executor-facing input port.
        request_inputs.pop("generation_intent")
    elif not dry_run:
        generation_intent = _automatic_generation_intent(
            capability,
            request_inputs,
            modality=intent_modality,
        )
        if intent_modality is not None and generation_intent is None:
            raise CapabilityValidationError(
                f"{capability.id} requires explicit generation_intent because its "
                "output cardinality is not safely inferable at admission"
            )

    from .actions import validate_action_inputs

    effective_inputs = dict(request_inputs)
    for port in capability.inputs if hasattr(capability, "inputs") else ():
        if port.name not in effective_inputs and port.default is not None:
            effective_inputs[port.name] = port.default
    validate_action_inputs(capability, effective_inputs)

    if child_mode:
        if dry_run or capability.capability_type != "executor" or argv or orchestrator_args:
            raise CapabilityValidationError("child bridge requires an executable action without launcher controls")
        _child_bridge._strict_json(effective_inputs)
        metadata = capability.definition.get("metadata", {})
        repeatable_string_inputs = _repeatable_string_input_names(capability)
        for name, value in effective_inputs.items():
            if type(value) is not list:
                continue
            if name not in repeatable_string_inputs:
                continue
            if any(type(item) is not str for item in value):
                raise CapabilityValidationError(
                    f"child input {name!r} repeatable values must be strings"
                )
        if "action_inputs_schema" not in metadata:
            unknown = set(effective_inputs) - {port.name for port in capability.inputs}
            if unknown:
                raise CapabilityValidationError("child invocation contains undeclared inputs")
            for port in capability.inputs:
                value = effective_inputs.get(port.name)
                if value is None:
                    continue
                if type(value) is list and port.name in repeatable_string_inputs:
                    continue
                accepted = {"string": (str,), "path": (str,), "directory": (str,), "html": (str,),
                            "boolean": (bool,), "integer": (int,), "number": (int, float)}.get(port.type)
                if accepted is not None and type(value) not in accepted:
                    raise CapabilityValidationError(f"child input {port.name!r} must match declared {port.type} type")
        effective_inputs = _validate_manifest_preview_inputs(
            capability, inputs=effective_inputs, orchestrator_args=(),
        )
        return _invoke_bridge_child(
            capability, registries=registries, inputs=effective_inputs,
            bridge=_child_bridge._bridge, child_key=child_key,
            wait=wait, timeout_seconds=timeout_seconds, poll_seconds=poll_seconds,
        )

    if isinstance(project, str) and not project.strip():
        project = None
    project_scope = _project_scope(capability)
    if not dry_run and project is None and project_scope == "required":
        project = _runtime_selected_project(_client)
        if project is None:
            from astrid.core.project.guidance import format_project_required_guidance

            raise CapabilityPreconditionError(
                format_project_required_guidance(
                    operation=f"{capability.capability_type} invocation"
                )
            )
    if variant_context is not None:
        resolved_variant = _resolve_generation_variant(
            _client,
            project=project,
            variant_of=variant_context["variant_of"],
            primary=variant_context["primary"],
        )
        variant_context = resolved_variant

    # Media handles (reference names, prior outputs, digests) and the
    # generation ``references=[{ref, role}]`` sugar resolve here, inside the
    # project, to the same managed descriptors callers used to hand-build.
    from .media_handles import apply_media_handles

    request_inputs, media_lineage, reference_links = apply_media_handles(
        _client,
        project,
        capability,
        request_inputs,
        resolve=_client is not None and bool(project),
    )

    # Validate the public selector/format contract before the runner can
    # create a ledger row or spawn a subprocess.  The runner repeats these
    # checks for direct CLI callers, but SDK callers should get the same
    # actionable typed error at admission time.
    invocation_authority_context: dict[str, Any] | None = None
    invocation_admission_metadata: dict[str, Any] | None = None
    invocation_storage_estimate: dict[str, int] | None = None
    if idempotency_context is not None:
        if not isinstance(idempotency_context, Mapping):
            raise CapabilityValidationError("idempotency_context must be an object")
        invocation_authority_context = _json_safe_mapping(dict(idempotency_context))
    if capability.id == "generation.generate_image_codex":
        count = request_inputs.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 4:
            raise CapabilityValidationError("Codex count must be an integer between 1 and 4")
        invocation_storage_estimate = {"scratch_bytes": 536870912, "output_bytes": 269484032}
    # These are managed-authority checks, not manifest checks.  A dry-run is
    # intentionally limited to the source ledger and therefore cannot inspect
    # a project tree or materialize a render snapshot.
    if not dry_run:
        if capability.id == "rendering.timeline_visualize":
            invocation_authority_context = _validate_timeline_visualize_inputs(
                inputs,
                project=project,
                project_root=project_root,
                out=out,
                _client=_client,
            )
            if invocation_authority_context.get("mode") == "revision_diff":
                inputs = dict(inputs or {})
                inputs["revision_diff"] = invocation_authority_context["revision_diff"]
            transcript_input = invocation_authority_context.get("transcript_input")
            if transcript_input is not None:
                inputs = dict(inputs or {})
                if inputs.get("transcript.json") not in (None, ""):
                    raise CapabilityValidationError(
                        "transcript input is host-owned; config.app.transcript supplies the CAS object"
                    )
                inputs["transcript.json"] = transcript_input
            if invocation_authority_context.get("mode") in {"filmstrip", "input_only", "composed_capture"}:
                # Only preflight may turn a successful project-owned render
                # into a file input. Public paths were rejected above.
                inputs = dict(inputs or {})
                authority_snapshot = (
                    invocation_authority_context.get("filmstrip_snapshot")
                    or invocation_authority_context.get("input_snapshot")
                    or invocation_authority_context.get("capture_snapshot")
                    or {}
                )
                inputs["project_slug"] = authority_snapshot["project_slug"]
                if invocation_authority_context.get("mode") == "filmstrip":
                    inputs["rendered_video"] = {
                        "digest": invocation_authority_context["video_digest"],
                        "object_id": invocation_authority_context["video_object_id"],
                    }
                authority_json = json.dumps(
                    invocation_authority_context, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False,
                )
                # The authority embeds the whole frozen timeline snapshot (1-2 MB),
                # which used to be inlined twice per task and copied into the run
                # and the admission receipt. Store the authority and the
                # snapshot's timeline and registry as managed objects instead;
                # the host materializes them as the executor's input files.
                stored = _store_visualize_objects(
                    _client, project, authority_json, authority_snapshot,
                )
                if stored is None:
                    inputs["filmstrip_authority"] = authority_json  # a client without import_bytes
                else:
                    inputs.update(stored)
                    invocation_authority_context = {
                        **invocation_authority_context,
                        "authority_object": stored["filmstrip_authority"]["digest"],
                    }
            # Preflight may add host-owned transcript/video bindings. Forward
            # those enriched inputs to kernel admission; retaining the initial
            # caller mapping would desynchronize the filmstrip identity guard.
            request_inputs = dict(inputs or {})
        elif capability.id == "rendering.render":
            inputs, invocation_authority_context = _prepare_managed_render_inputs(
                inputs,
                project=project,
                _client=_client,
            )
            # Managed render admission adds the frozen snapshot and authority
            # after the initial request copy above. Keep the task payload in
            # sync so the Runtime host can materialize the attempt-local
            # timeline before expanding the renderer command.
            request_inputs = dict(inputs)
            if invocation_authority_context is not None:
                snapshot = (inputs or {}).get("timeline_snapshot")
                snapshot_config = snapshot.get("config") if isinstance(snapshot, Mapping) else None
                snapshot_registry = snapshot.get("registry") if isinstance(snapshot, Mapping) else None
                if not isinstance(snapshot_config, Mapping) or not isinstance(snapshot_registry, Mapping):
                    raise CapabilityInvocationError(
                        "managed render storage estimation requires the expanded canonical snapshot"
                    )
                from astrid.core.rendering.storage import (
                    StorageEstimateError,
                    estimate_managed_render_storage,
                    managed_object_sizes,
                    used_effect_asset_sizes,
                )
                from astrid.sdk.pagination import paged_rows

                media_rows = paged_rows(_client.media.list, str(project), limit=50)
                if media_rows is None:
                    raise CapabilityInvocationError(
                        "runtime media listing is unavailable for exact render storage estimation"
                    )
                try:
                    exact_object_sizes = managed_object_sizes(snapshot_registry, media_rows)
                    effect_sizes = used_effect_asset_sizes(snapshot_config)
                    storage_estimate = estimate_managed_render_storage(
                        timeline=snapshot_config,
                        registry=snapshot_registry,
                        object_sizes=exact_object_sizes,
                        effect_asset_sizes=effect_sizes,
                        requested_profile=(inputs or {}).get("profile"),
                        review_render=bool((inputs or {}).get("review")),
                    )
                except StorageEstimateError as exc:
                    raise CapabilityValidationError(str(exc)) from exc
                _assert_render_disk_preflight(storage_estimate)
                invocation_admission_metadata = {
                    "storage_estimate": storage_estimate,
                    "runtime_enforced": True,
                }
                invocation_storage_estimate = {
                    "scratch_bytes": int(storage_estimate["estimated_scratch_bytes"]),
                    "output_bytes": int(storage_estimate["estimated_output_bytes"]),
                }

    # Generation requests have a single read-only preflight for both dry-run
    # and live invocation.  This keeps generic ``sdk.invoke`` from accepting
    # an impossible model/mode/backend cell (or FLF request missing its end
    # frame) and discovering the problem only after kernel admission.
    modality = {
        "generation.generate_image": "image",
        "generation.generate_image_codex": "image",
        "generation.generate_video": "video",
        "generation.generate_audio": "audio",
    }.get(str(capability.id))
    if modality is not None:
        model_registry = sdk_module._load_model_registry(
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
        )
        from astrid.core.generation.preflight import (
            require_local_generation_readiness,
            validate_generation_request,
        )

        if capability.id == "generation.generate_image":
            recipe = request_inputs.get("shot_generation_recipe")
            if recipe is not None:
                from astrid.packs.generation.actions.generate_image.task_adapter import (
                    GenerateImageAdapterError,
                    validate_shot_generation_recipe,
                )

                try:
                    validate_shot_generation_recipe(
                        recipe,
                        model=request_inputs.get("model"),
                        mode=request_inputs.get("mode"),
                        execution=request_inputs.get("execution"),
                        resolved_settings=request_inputs,
                    )
                except GenerateImageAdapterError as exc:
                    raise CapabilityValidationError(str(exc)) from exc

        model_entry, _mode_spec = validate_generation_request(
            model_registry,
            model=request_inputs.get("model"),
            mode=request_inputs.get("mode"),
            execution=request_inputs.get("execution"),
            inputs=request_inputs,
            modality=modality,
        )
        if request_inputs.get("execution") == "local":
            require_local_generation_readiness(
                model_entry,
                request_inputs["mode"],
                python_executable=python_exec,
            )

    # Ledger exemption: dry_run never admitted.  The preview is built from the
    # already resolved manifest DTO and does not import either runner or any
    # local project/run authority.
    if dry_run:
        try:
            raw_result, preview_ok = _manifest_dry_run_result(
                capability,
                inputs=request_inputs,
                outputs=outputs,
                brief=brief,
                python_exec=python_exec,
                out=out,
                orchestrator_args=tuple(orchestrator_args),
            )
        except AstridSDKError:
            raise
        except Exception as exc:
            mapped = _sdk_error_from_exception(exc)
            if mapped is not None:
                raise mapped from exc
            raise CapabilityInvocationError(
                f"failed to invoke {capability.capability_type} {capability.id!r}"
            ) from exc
        error = raw_result.get("error") if isinstance(raw_result.get("error"), Mapping) else None
        manifest_path = None
        run_id_raw = None
        run_root_raw = None
        executor_version_raw = raw_result.get("executor_version")
        return InvocationResult(
            capability_id=capability.id,
            capability_type=capability.capability_type,
            native_kind=capability.native_kind,
            ok=preview_ok,
            error=error,
            manifest_path=manifest_path,
            raw_result=raw_result,
            run_id=run_id_raw if isinstance(run_id_raw, str) and run_id_raw else None,
            run_root=str(Path(run_root_raw).expanduser().resolve())
            if isinstance(run_root_raw, str) and run_root_raw
            else None,
            outputs={},
            executor_version=executor_version_raw
            if isinstance(executor_version_raw, str) and executor_version_raw
            else None,
            kernel_run_id=None,
            kernel_task_id=None,
            kernel_attempt_id=None,
        )

    if _recovery is not None and capability.capability_type != "executor":
        raise CapabilityValidationError("recovery requires a Runtime task executor")

    if capability.capability_type == "orchestrator":
        if validated_child_policy is not None:
            raise CapabilityValidationError("child_delegation requires a Runtime-admitted executor action")
        # The Runtime task registry is the executor/worker surface.  A
        # parent orchestrator is the public launcher that coordinates those
        # registered child tasks and owns the caller's output directory.
        return _invoke_local_orchestrator(
            capability,
            project=project,
            inputs=request_inputs,
            outputs=outputs,
            out=out,
            brief=brief,
            python_exec=python_exec,
            verbose=verbose,
            orchestrator_args=tuple(orchestrator_args),
        )

    # Project requirements were resolved above. Public knowledge reads may
    # enter the same runtime admission path without a project association.
    kernel_capability_version: str | None = None
    if capability.capability_type == "executor":
        from astrid.core.foundation.hash import executor_definition_digest

        executor_registry, _, _ = registries
        kernel_capability_version = executor_definition_digest(executor_registry.get(capability.id))
        invocation_authority_context = dict(invocation_authority_context or {})
        invocation_authority_context["executor_version"] = kernel_capability_version
        if media_lineage:
            # Receipted with the task: which handle (and reference) fed each port.
            invocation_authority_context["media_handles"] = media_lineage
        if reference_links:
            invocation_authority_context["reference_links"] = reference_links
    kr = kt = ka = ""
    try:
        # Keep the private seam backwards-compatible for callers that replace
        # it with a narrow test double, while still forwarding an explicitly
        # composed registry for long-lived clients.  ``None`` means the
        # kernel will build its normal standard composition; passing it as a
        # keyword adds no information and needlessly breaks older doubles.
        kernel_kwargs: dict[str, Any] = {
            "kind": kind,
            "project": project,
            "inputs": request_inputs,
            "outputs": outputs,
            "out": out,
            "brief": brief,
            "python_exec": python_exec,
            "orchestrator_args": tuple(orchestrator_args),
            "extra_pack_roots": extra_pack_roots,
            "idempotency_context": invocation_authority_context,
            "admission_metadata": invocation_admission_metadata,
            "storage_estimate": invocation_storage_estimate,
        }
        if _recovery is not None:
            kernel_kwargs["_recovery"] = _recovery
            kernel_kwargs["_recovery_metadata"] = {
                "capability_id": capability.id, "capability_type": capability.capability_type,
                "native_kind": capability.native_kind, "executor_version": kernel_capability_version,
                "discover_managed_outputs": False,
                "read_managed_outputs": capability.capability_type == "executor"
                    and (read_managed_outputs or intent_modality is not None),
                "out": str(out) if out is not None else None, "project": project,
                "authority_context": invocation_authority_context,
            }
        if generation_intent is not None:
            kernel_kwargs["generation_intent"] = generation_intent
        if variant_context is not None:
            kernel_kwargs["variant_context"] = variant_context
        if normalized_execution_request is not None:
            kernel_kwargs["execution_request"] = normalized_execution_request
        if validated_child_policy is not None:
            kernel_kwargs["child_delegation"] = validated_child_policy
        if registry is not None:
            kernel_kwargs["registry"] = registry
        if rerun:
            kernel_kwargs["rerun"] = True
        kr, kt, ka, mpath, raw_result, ok, _ = _kernel_invoke(
            capability,
            **kernel_kwargs,
            _client=_client,
        )
        replay_info = raw_result.get("replayed") if isinstance(raw_result, Mapping) else None
        expects_outputs = _capability_declares_outputs(capability)
        return _observe_task_invocation(
            _client,
            capability_id=capability.id,
            capability_type=capability.capability_type,
            native_kind=capability.native_kind,
            task_id=kt,
            run_id=kr,
            initial_raw_result=raw_result if isinstance(raw_result, Mapping) else None,
            initial_ok=ok,
            attempt_id=ka,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            # A declared output must be listable before the task reports ok (F-053).
            read_managed_outputs=(
                capability.capability_type == "executor"
                and (read_managed_outputs or intent_modality is not None)
            ) or expects_outputs,
            expects_outputs=expects_outputs,
            replay_info=replay_info,
            discover_managed_outputs=_recovery is None,
            executor_version=kernel_capability_version,
            out=out,
            project=project,
            authority_context=invocation_authority_context,
            manifest_path=mpath,
        )
    except AstridSDKError:
        raise
    except Exception as exc:
        mapped = _sdk_error_from_exception(exc)
        if mapped is not None:
            raise mapped from exc
        raise CapabilityInvocationError(
            f"failed to invoke {capability.capability_type} {capability.id!r}: "
            f"{type(exc).__name__}: {_redact_message(str(exc))}",
            details={
                "cause_type": type(exc).__name__,
                "cause_message": _redact_message(str(exc)),
                "kernel_run_id": kr if isinstance(kr, str) and kr else None,
                "kernel_task_id": kt if isinstance(kt, str) and kt else None,
                "kernel_attempt_id": ka if isinstance(ka, str) and ka else None,
            },
        ) from exc


def invoke_result(
    capability_id: str,
    *,
    kind: Any,
    **kwargs: Any,
) -> InvocationResult:
    """Invoke while keeping typed preflight failures in the result contract.

    ``invoke`` remains the exception-oriented API for callers that want typed
    recovery branches.  Maker-facing agents that need one uniform JSON-safe
    branch can use this sibling: validation/precondition failures raised before
    kernel admission become an ``InvocationResult(ok=False)`` with the same
    ``error`` mapping used by a post-admission failure.  No run, task, staging
    directory, network call, or provider request is created by this adapter.
    """

    if "_include_internal" in kwargs:
        raise TypeError("_include_internal is reserved for Astrid's private dispatch")
    return _invoke_result(capability_id, kind=kind, include_internal=False, kwargs=kwargs)


def _invoke_internal_result(
    capability_id: str,
    *,
    kind: Any,
    **kwargs: Any,
) -> InvocationResult:
    """Invoke a private backend used by a canonical product operation."""
    return _invoke_result(capability_id, kind=kind, include_internal=True, kwargs=kwargs)


def _invoke_result(
    capability_id: str,
    *,
    kind: Any,
    include_internal: bool,
    kwargs: Mapping[str, Any],
) -> InvocationResult:
    try:
        return invoke(
            capability_id,
            kind=kind,
            _include_internal=include_internal,
            _internal_dispatch_token=(
                _INTERNAL_DISPATCH_TOKEN if include_internal else None
            ),
            **dict(kwargs),
        )
    except AstridSDKError as exc:
        category = getattr(exc, "category", "invocation")
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "sdk_error": type(exc).__name__,
            "sdk_category": category,
        }
        details = getattr(exc, "details", None)
        if isinstance(details, Mapping) and details:
            error["details"] = _json_safe(dict(details))
            if category == "validation":
                error["validation"] = _json_safe(dict(details))
        return InvocationResult(
            capability_id=capability_id,
            capability_type=kind if kind in ("executor", "orchestrator") else "executor",
            native_kind="unknown",
            ok=False,
            error=error,
            raw_result={"ok": False, "error": error},
            run_id=details.get("kernel_run_id") if isinstance(details, Mapping) else None,
            kernel_run_id=details.get("kernel_run_id") if isinstance(details, Mapping) else None,
            kernel_task_id=details.get("kernel_task_id") if isinstance(details, Mapping) else None,
            kernel_attempt_id=details.get("kernel_attempt_id") if isinstance(details, Mapping) else None,
        )
