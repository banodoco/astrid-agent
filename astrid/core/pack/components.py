"""Pack-v3 clip modifier admission for the existing Composition/App host.

No contributor code executes here. Binding returns a rechecked source closure for
an existing trusted local host/bundler, never a second renderer or Python plugin.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import jsonschema
import yaml

from astrid.sdk.timeline.commands import CommandError

POINT = "timeline.component"
_SCHEMA = Path(__file__).parent / "schemas/component.json"


class UnsupportedComponent(CommandError):
    code = "unsupported"


@dataclass(frozen=True)
class PackComponent:
    identity: str
    implementation_digest: str
    pack_root: Path
    descriptor_path: str
    descriptor: Mapping[str, Any]
    source_kind: str
    eligible: bool
    eligibility_reason: str

    def to_json(self):
        return {"identity": self.identity, "implementation_digest": self.implementation_digest,
                "descriptor": copy.deepcopy(dict(self.descriptor)), "source_kind": self.source_kind,
                "eligible": self.eligible, "eligibility_reason": self.eligibility_reason}


def _declarations(root, definition, resources):
    handles = {item.path: item for item in resources}
    schema = json.loads(_SCHEMA.read_text())
    result, seen = [], set()
    for path in definition.extensions.get("timeline", {}).get("components", ()):
        handle = handles.get(path)
        if handle is None or handle.file_kind != "file":
            raise ValueError(f"component descriptor {path!r} must be a declared file resource")
        payload = yaml.safe_load(handle.resolved.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)
        jsonschema.Draft202012Validator.check_schema(payload["params"])

        def local_refs(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"$ref", "$dynamicRef"} and (not isinstance(child, str) or not child.startswith("#")):
                        raise ValueError("component params schema supports only local JSON references")
                    local_refs(child)
            elif isinstance(value, list):
                for child in value:
                    local_refs(child)
        local_refs(payload["params"])
        identity = f"{POINT}/{payload['id']}"
        if not payload["id"].startswith(definition.id + "."):
            raise ValueError(f"component {payload['id']!r} must belong to pack {definition.id!r}")
        if identity in seen:
            raise ValueError(f"duplicate component identity {identity}")
        seen.add(identity)
        code = handles.get(payload["implementation"]["path"])
        if code is None or code.file_kind != "file" or code.resolved.suffix not in {".ts", ".tsx"}:
            raise ValueError("component implementation must be a declared TypeScript file resource")
        closure = {item.path: item.sha256 for item in resources if item.file_kind == "file"}
        digest = hashlib.sha256(json.dumps({"identity": identity, "declaration": payload,
            "resources": closure}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result.append((path, payload, digest))
    return result


def validate_component_declarations(root, definition, resources):
    from astrid.core.pack.canonical import CanonicalPackValidationError
    try:
        _declarations(root, definition, resources)
    except (ValueError, TypeError, KeyError, yaml.YAMLError, jsonschema.SchemaError,
            jsonschema.ValidationError) as exc:
        raise CanonicalPackValidationError(f"invalid timeline component: {exc}") from exc


def discover_components(*, project_root=None, extra_pack_roots=()):
    from astrid.core.foundation.paths import REPO_ROOT
    from astrid.core.pack.canonical import validate_canonical_pack
    from astrid.core.pack.discovery import discover_pack_metadata
    from astrid.core.rendering.registry import _derive_pack_trust
    found = {}
    for item in discover_pack_metadata(
        project_root=REPO_ROOT if project_root is None else project_root,
        extra_pack_roots=tuple(str(p) for p in extra_pack_roots),
    ):
        if str(item.pack.schema_version) != "3" or not item.pack.extensions.get("timeline", {}).get("components"):
            continue
        admitted = validate_canonical_pack(item.pack.root)
        trust = _derive_pack_trust(item)
        for path, payload, digest in _declarations(admitted.root, admitted.definition, admitted.resources):
            identity = f"{POINT}/{payload['id']}"
            candidate = PackComponent(identity, digest, admitted.root, path, payload,
                                      item.source_kind, trust.eligible, trust.reason)
            if identity not in found or (not found[identity].eligible and candidate.eligible):
                found[identity] = candidate
    return tuple(found.values())


def resolve_component(identity, **discovery):
    if not isinstance(identity, str) or not identity.startswith(POINT + "/"):
        raise UnsupportedComponent(f"unsupported component identity: {identity!r}; use {POINT}/<pack>.<key>")
    for component in discover_components(**discovery):
        if component.identity == identity:
            return component
    raise UnsupportedComponent(f"unsupported unknown component: {identity}")


def bind_component(component, *, host):
    """Re-admit exact identity/closure for the existing trusted local TS loader.

    This returns bytes, not executable authority. The receiving host must use its
    existing plugin boundary, bind the declared export and inject the resulting
    declaration in its exact-key modifierRegistry. No source-tree scan/import.
    """
    from astrid.core.pack.canonical import validate_canonical_pack
    if host not in {"composition", "app"}:
        raise UnsupportedComponent(f"unsupported timeline component host: {host}")
    if not component.eligible:
        raise UnsupportedComponent(f"unsupported execution: {component.eligibility_reason}")
    admitted = validate_canonical_pack(component.pack_root)
    current = next((row for row in _declarations(admitted.root, admitted.definition, admitted.resources)
                    if f"{POINT}/{row[1]['id']}" == component.identity), None)
    if current is None or current[2] != component.implementation_digest:
        raise CommandError("component implementation changed; rediscover before binding")
    declaration = current[1]
    if host not in declaration["hosts"]:
        raise UnsupportedComponent(f"unsupported component host: {host}")
    sources = {}
    for resource in admitted.resources:
        if resource.file_kind == "file":
            data = resource.resolved.read_bytes()
            if hashlib.sha256(data).hexdigest() != resource.sha256:
                raise CommandError("component implementation changed during binding")
            sources[resource.path] = {"sha256": resource.sha256, "text": data.decode("utf-8")}
    return {"identity": component.identity, "implementation_digest": current[2],
            "descriptor": copy.deepcopy(declaration), "sources": sources}


def component_instance(component, instance_id, params, *, host):
    """Construct a validated stable modifier reference; does not mutate a timeline."""
    binding = bind_component(component, host=host)
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise CommandError("component instance id must be nonempty")
    try:
        value = json.loads(json.dumps(params, allow_nan=False))
        jsonschema.Draft202012Validator(binding["descriptor"]["params"]).validate(value)
    except (TypeError, ValueError, jsonschema.ValidationError) as exc:
        raise CommandError(f"invalid component params: {exc}") from exc
    return {"id": instance_id, "element": component.identity, "params": value}


def component_bounds(component, target_bounds):
    """Shared evaluation projection: no guessed bounds for undeclared geometry."""
    binding = bind_component(component, host="composition")
    if binding["descriptor"].get("bounds") != "preserve":
        return {"status": "unknown", "finding": {"code": "unknown-modifier-bounds",
                "element": component.identity, "reason": "Component declares no bounds"}}
    return copy.deepcopy(target_bounds)


# Discovery configuration is scoped to a trusted consumer, never browser input.
# Store roots rather than declarations: every edit re-resolves and rebinds.
_DISCOVERY = ContextVar("component_discovery", default={})


@contextmanager
def component_discovery_scope(**discovery):
    token = _DISCOVERY.set(discovery)
    try:
        yield
    finally:
        _DISCOVERY.reset(token)


def validate_component_reference(row):
    component = resolve_component(row["element"], **_DISCOVERY.get())
    component_instance(component, row["id"], row["params"], host="composition")


def component_add_call(component, *, select, instance_id, params, host):
    row = component_instance(component, instance_id, params, host=host)
    path = "modifiers[" + json.dumps(instance_id) + "]"
    return {"op": "clip.set-param", "select": select,
            "args": {"path": path, "value": row}, "expect": {path: {"state": "absent"}}}
