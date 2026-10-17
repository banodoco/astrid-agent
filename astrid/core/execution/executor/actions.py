"""Normalize admitted pack action declarations for core executor discovery.

This reads contracts only. Invocation implementations remain unresolved until
execution. Both raw core defaults and the SDK consume these definitions.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any

from astrid.core.contracts.exec_error import ExecError

from .schema import ExecutorDefinition, ExecutorValidationError


def _json_safe(value: Any) -> Any:
    """Return a recursively JSON-safe copy of *value*."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, ExecError):
        return {
            "code": value.code,
            "type": value.type,
            "message": value.message,
            "recovery": value.recovery,
        }
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        # Sets are accepted by a few repeatable SDK inputs for parity with
        # the CLI, but their iteration order is hash-seed dependent.  Emit a
        # sorted JSON array so result.to_dict() is both serializable and
        # stable across fresh Python processes.
        return [
            _json_safe(item)
            for item in sorted(value, key=lambda item: (type(item).__name__, str(item)))
        ]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if is_dataclass(value):
        return {field.name: _json_safe(getattr(value, field.name)) for field in fields(value)}
    return value


def _json_safe_mapping(value: Any) -> dict[str, Any]:
    payload = _json_safe(value)
    if not isinstance(payload, dict):
        raise TypeError(f"expected mapping payload, got {type(payload).__name__}")
    return payload


def _contract(value: Any, root: Path) -> tuple[Any, str]:
    from astrid.core.pack.manifest import load_manifest_payload

    base = root.resolve().as_uri() + "/"
    if isinstance(value, str):
        path = (root / value).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ExecutorValidationError("action contract escapes pack root")
        return load_manifest_payload(path, manifest_kind="JSON Schema"), path.as_uri()
    return value, base


def _schema_input_ports(schema: Any) -> list[dict[str, Any]]:
    """Project object properties for existing binding; retain the full schema."""
    if not isinstance(schema, Mapping):
        return []
    required = schema.get("required", ())
    ports = []
    for name, property_schema in schema.get("properties", {}).items():
        item = property_schema if isinstance(property_schema, Mapping) else {}
        port = {"name": name, "type": item.get("type", "json")}
        if port["type"] not in ("string", "integer", "number", "boolean"):
            port["type"] = "json"
        if "description" in item:
            port["description"] = item["description"]
        port["required"] = name in required
        if "default" in item:
            port["default"] = item["default"]
        ports.append(port)
    return ports


def _action_definition(discovered_pack: Any, local_id: str, action: Mapping[str, Any]) -> ExecutorDefinition:
    """Return an ExecutorDefinition without importing action implementation code."""
    from astrid.core.execution.executor.schema import validate_executor_definition

    pack = getattr(discovered_pack, "pack", discovered_pack)
    declaration = _json_safe_mapping(action)
    invocation = declaration["invocation"]
    metadata = dict(declaration.get("metadata", {}))
    metadata.update({
        "source": "pack",
        "source_pack": pack.id,
        "pack_root": str(pack.root),
        "priority": 30,
        "action_declaration": declaration,
        "action_invocation": invocation,
    })
    # This marker restricts dispatch; it never grants document authority.
    # Reserve the selected identity even for a lookalike pack declaration.
    metadata["requires_caller_document_adapter"] = (
        f"{pack.id}.{local_id}" == "media.transcript_publish"
    )
    if "external_runtime" in declaration:
        metadata["external_runtime"] = declaration["external_runtime"]
    definition = {
        key: declaration[key]
        for key in ("description", "short_description", "keywords", "cache", "isolation",
                    "graph", "conditions", "clip_kinds_supported", "pipeline_requirements", "scoped_configs")
        if key in declaration
    }
    definition.update(id=f"{pack.id}.{local_id}", name=declaration.get("name", local_id),
                      version=declaration.get("version", pack.version),
                      kind="external" if invocation["kind"] == "command" else "built_in",
                      metadata=metadata)
    for slot in ("inputs", "outputs"):
        value = declaration.get(slot, [])
        if isinstance(value, list):
            definition[slot] = value
        else:
            schema, base = _contract(value, pack.root)
            metadata[f"action_{slot}_schema"] = schema
            metadata[f"action_{slot}_schema_base"] = base
            # A return schema is a value contract, not a list of files to
            # harvest. Only explicitly declared Output arrays enter the host's
            # output-publication contract.
            definition[slot] = _schema_input_ports(schema) if slot == "inputs" else []
    if invocation["kind"] == "command":
        definition["command"] = invocation["command"]
    admitted = validate_executor_definition(definition)
    if isinstance(declaration.get("inputs"), list):
        # Publish the already validated executor semantics, including the
        # canonical required=True default. A catalog must not force consumers
        # to guess a missing boolean from an otherwise admitted action.
        required = {port.name: port.required for port in admitted.inputs}
        normalized = {**declaration, "inputs": [
            {**port, "required": required[port["name"]]} for port in declaration["inputs"]]}
        admitted = replace(admitted, metadata={**admitted.metadata, "action_declaration": normalized})
    return admitted


def action_is_local_orchestrator(action: Mapping[str, Any]) -> bool:
    """Validate the V3 command's explicit local-parent disposition."""
    metadata = action.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ExecutorValidationError("action metadata must be an object")
    marker = metadata.get("local_orchestrator", False)
    if type(marker) is not bool:
        raise ExecutorValidationError("action metadata.local_orchestrator must be a boolean")
    if marker and action.get("invocation", {}).get("kind") != "command":
        raise ExecutorValidationError("local orchestrator action requires a command invocation")
    return marker


def action_executor_definition(discovered_pack: Any, local_id: str, action: Mapping[str, Any]) -> ExecutorDefinition:
    if action_is_local_orchestrator(action):
        raise ExecutorValidationError("local orchestrator action is not a host executor")
    return _action_definition(discovered_pack, local_id, action)


def action_orchestrator_definition(discovered_pack: Any, local_id: str, action: Mapping[str, Any]):
    """Project one admitted V3 local parent without resolving its implementation."""
    from astrid.core.execution.orchestrator.schema import (
        OrchestratorDefinition, RuntimeSpec, validate_orchestrator_definition,
    )
    if not action_is_local_orchestrator(action):
        raise ExecutorValidationError("action is not a local orchestrator")
    definition = _action_definition(discovered_pack, local_id, action)
    graph = action.get("graph", {})
    return validate_orchestrator_definition(OrchestratorDefinition(
        id=definition.id, name=definition.name, kind="external", version=definition.version,
        runtime=RuntimeSpec(kind="command", command=definition.command),
        description=definition.description, short_description=definition.short_description,
        keywords=definition.keywords, inputs=definition.inputs, outputs=definition.outputs,
        cache=definition.cache, isolation=definition.isolation,
        scoped_configs=definition.scoped_configs, metadata=definition.metadata,
        child_executors=tuple(graph.get("child_actions", ())),
        child_orchestrators=tuple(graph.get("child_orchestrators", ())),
    ))
