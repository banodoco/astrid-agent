"""Prepare bounded Runtime resolution data from an admitted pack declaration.

This is a preparation-time channel. It does not discover an entry, read user
settings through a worker credential, or import a pack's resolver. A pack may
declare ``metadata.runtime_resolution`` (one declaration or a list), containing
an already described scalar entry, ordinary admission data and field bindings.
LS-8's captured helper can supply the same data at generation changes.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping


FORMAT = "runtime.resolution/v1"
SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "json", "object", "array"})
PORT_FIELDS = frozenset({"name", "type", "required", "default", "aliases", "choices", "min", "max", "_explicit_required"})


class ResolutionUnsupported(ValueError):
    """Use ordinary caller preparation before admission for this entry."""


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def prepare_descriptor(declaration, *, capability_id, capability_digest, source_digest, source_epoch):
    """Project a source-captured declaration; the host owns all source identities.

    ``settings_snapshot`` is optional only for settings-independent entries.
    Otherwise it is the caller-authorized app-state snapshot (actor_id, key,
    version, content). Runtime rechecks that exact scoped revision at admission
    and claim. A worker cannot substitute another user's settings at call time.
    """
    from astrid.core.generation.entries import _input_type, validate_inputs
    from astrid.sdk.execution_request import normalize_execution_request
    from astrid.sdk.generation import GenerationInputError

    required = {"entry", "admission", "bindings", "placement_generation", "project_id"}
    optional = {"placements", "default_placement", "seed_parameter", "refusal", "settings_snapshot"}
    if not isinstance(declaration, Mapping) or not required <= declaration.keys() or set(declaration) - required - optional:
        raise ResolutionUnsupported("resolution declaration needs supported data-only fields")
    entry = declaration["entry"]
    if (not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str)
            or not isinstance(entry.get("inputs"), list) or entry.get("hooks_declared")
            or entry.get("requires_fresh_io") or entry.get("defaults")):
        raise ResolutionUnsupported("entry needs full preparation before admission")
    ports = []
    for raw in entry["inputs"]:
        if not isinstance(raw, Mapping) or set(raw) - PORT_FIELDS - {"description"}:
            raise ResolutionUnsupported("entry has executable, native-node or file input resolution")
        port = {k: copy.deepcopy(v) for k, v in raw.items() if k in PORT_FIELDS}
        port["type"] = _input_type(raw.get("type"))
        if port["type"] not in SCALAR_TYPES:
            raise ResolutionUnsupported("entry has an input that requires full preparation")
        ports.append(port)
    description = {**entry, "inputs": ports}
    defaults = {p["name"]: p["default"] for p in ports if "default" in p}
    try:
        validate_inputs(description, defaults, required=False)
    except ValueError as exc:
        raise ResolutionUnsupported("entry defaults do not satisfy its full-path contract") from exc
    admission = copy.deepcopy(dict(declaration["admission"]))
    if admission.get("execution_request") is not None:
        request = normalize_execution_request(admission["execution_request"])
        if request.get("inputs"):
            raise ResolutionUnsupported("admitted input transfer requires full preparation")
        admission["execution_request"] = request
    error = GenerationInputError("$input_error", entry=dict(entry))
    error_template = {"code": error.code, "message": str(error), "details": copy.deepcopy(error.details)}
    details = error_template["details"]["readiness"]["issues"][0]["details"]
    details.update(field="$input_field", fields="$input_fields")
    descriptor = {
        "format": FORMAT, "entry_id": entry["id"],
        "capability_id": capability_id, "capability_digest": capability_digest,
        "source_digest": source_digest, "source_epoch": source_epoch,
        "settings_digest": _digest(None),
        "placement_generation": declaration["placement_generation"],
        "project_id": declaration["project_id"], "inputs": ports,
        "admission": admission, "bindings": copy.deepcopy(declaration["bindings"]),
        "input_error": error_template,
    }
    if entry.get("mode") is not None:
        descriptor["mode"] = entry["mode"]
    for key in optional - {"settings_snapshot"}:
        if key in declaration:
            descriptor[key] = copy.deepcopy(declaration[key])
    snapshot = declaration.get("settings_snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, Mapping) or not {"actor_id", "key", "version", "content"} <= snapshot.keys():
            raise ResolutionUnsupported("settings require an exact authorized Runtime snapshot")
        descriptor["settings_ref"] = {"actor": snapshot["actor_id"], "key": snapshot["key"], "version": snapshot["version"]}
        descriptor["settings_digest"] = _digest(snapshot["content"])
    try:
        encoded = json.dumps(descriptor, allow_nan=False, separators=(",", ":"), ensure_ascii=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ResolutionUnsupported("resolution descriptor must be finite JSON data") from exc
    if len(encoded.encode()) > 65536:
        raise ResolutionUnsupported("resolution descriptor exceeds its bounded preparation channel")
    return descriptor


def registration_descriptors(records, registration):
    """Bind supported declarations to this exact captured host registration."""
    descriptors, fallbacks = [], []
    for record in records:
        if not record.ready:
            continue
        declarations = record.definition.metadata.get("runtime_resolution", [])
        if isinstance(declarations, Mapping):
            declarations = [declarations]
        if not isinstance(declarations, list) or len(declarations) > 64:
            raise ResolutionUnsupported("runtime_resolution must be a bounded declaration list")
        for declaration in declarations:
            try:
                descriptors.append(prepare_descriptor(declaration,
                    capability_id=record.id, capability_digest=record.capability_digest,
                    source_digest=registration["source_digest"], source_epoch=registration["source_epoch"]))
            except ResolutionUnsupported as exc:
                fallbacks.append({"capability_id": record.id, "reason": str(exc)})
    return descriptors, fallbacks
