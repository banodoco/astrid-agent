"""Command-only timeline operations admitted by the existing V3 pack route.

Discovery reads only declared resources; it never scans sidecar folders or uses
core.extensions discovery. Python runs only for an eligible, explicitly invoked
SDK operation. This trusted local host is not a Python security sandbox.
"""
from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import jsonschema
import yaml

from astrid.sdk.timeline.commands import CommandError, EditCommand

POINT = "timeline.operation"
_SCHEMA = Path(__file__).parent / "schemas" / "operation.json"
_DISCOVERY = ContextVar("operation_discovery", default={})


@contextmanager
def operation_discovery_scope(**discovery):
    """Trusted local discovery configuration; roots never come from OpCall args."""
    token = _DISCOVERY.set(discovery)
    try:
        yield
    finally:
        _DISCOVERY.reset(token)


class UnsupportedOperation(CommandError):
    code = "unsupported"


@dataclass(frozen=True)
class PackOperation:
    identity: str
    implementation_digest: str
    pack_root: Path
    descriptor_path: str
    descriptor: Mapping[str, Any]
    source_kind: str
    eligible: bool
    eligibility_reason: str

    def to_json(self):
        return {
            "identity": self.identity,
            "implementation_digest": self.implementation_digest,
            "descriptor": copy.deepcopy(dict(self.descriptor)),
            "source_kind": self.source_kind,
            "eligible": self.eligible,
            "eligibility_reason": self.eligibility_reason,
        }


def _declarations(root, definition, resources):
    """Use canonical handles: undeclared files and directory globs cannot export code."""
    handles = {item.path: item for item in resources}
    paths = definition.extensions.get("timeline", {}).get("operations", ())
    schema = json.loads(_SCHEMA.read_text())
    result = []
    seen = set()
    for path in paths:
        handle = handles.get(path)
        if handle is None or handle.file_kind != "file":
            raise ValueError(f"operation descriptor {path!r} must be a declared file resource")
        payload = yaml.safe_load(handle.resolved.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)
        jsonschema.Draft202012Validator.check_schema(payload["inputs"])
        def local_refs(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"$ref", "$dynamicRef"} and (not isinstance(child, str) or not child.startswith("#")):
                        raise ValueError("operation input schema supports only local JSON references")
                    local_refs(child)
            elif isinstance(value, list):
                for child in value:
                    local_refs(child)
        local_refs(payload["inputs"])
        identity = f"{POINT}/{payload['id']}"
        if not payload["id"].startswith(definition.id + "."):
            raise ValueError(f"operation {payload['id']!r} must belong to pack {definition.id!r}")
        if identity in seen:
            raise ValueError(f"duplicate operation identity {identity}")
        seen.add(identity)
        code = handles.get(payload["implementation"]["path"])
        if code is None or code.file_kind != "file" or code.resolved.suffix != ".py":
            raise ValueError("operation implementation must be a declared Python file resource")
        # Include the complete admitted pack closure, not just manifest metadata.
        # These hashes are also rechecked at invocation; no bytecode/import cache.
        closure = {item.path: item.sha256 for item in resources if item.file_kind == "file"}
        digest = hashlib.sha256(json.dumps({
            "identity": identity, "declaration": payload, "resources": closure,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result.append((path, payload, digest))
    return result


def validate_operation_declarations(root, definition, resources):
    """Canonical admission hook; validation and discovery execute no contribution code."""
    from astrid.core.pack.canonical import CanonicalPackValidationError
    try:
        _declarations(root, definition, resources)
    except (ValueError, TypeError, KeyError, yaml.YAMLError, jsonschema.SchemaError,
            jsonschema.ValidationError) as exc:
        raise CanonicalPackValidationError(f"invalid timeline operation: {exc}") from exc


def discover_operations(*, project_root=None, extra_pack_roots=()):
    """List point-qualified operations in existing pack precedence order."""
    from astrid.core.foundation.paths import REPO_ROOT
    from astrid.core.pack.canonical import validate_canonical_pack
    from astrid.core.pack.discovery import discover_pack_metadata
    from astrid.core.rendering.registry import _derive_pack_trust

    found = {}
    for item in discover_pack_metadata(
        project_root=REPO_ROOT if project_root is None else project_root,
        extra_pack_roots=tuple(str(p) for p in extra_pack_roots),
    ):
        if str(item.pack.schema_version) != "3":
            continue
        if not item.pack.extensions.get("timeline", {}).get("operations"):
            continue
        admitted = validate_canonical_pack(item.pack.root)
        trust = _derive_pack_trust(item)
        for path, payload, digest in _declarations(
            admitted.root, admitted.definition, admitted.resources,
        ):
            identity = f"{POINT}/{payload['id']}"
            candidate = PackOperation(identity, digest, admitted.root, path, payload,
                                      item.source_kind, trust.eligible, trust.reason)
            # An inspect-only declaration cannot displace an executable source.
            if identity not in found or (not found[identity].eligible and candidate.eligible):
                found[identity] = candidate
    return tuple(found.values())


def resolve_operation(identity, **discovery):
    """Resolve only a complete point-qualified identity; never a basename."""
    if not isinstance(identity, str) or not identity.startswith(POINT + "/"):
        raise UnsupportedOperation(f"unsupported operation identity: {identity!r}; use {POINT}/<pack>.<key>")
    for operation in discover_operations(**(_DISCOVERY.get() if not discovery else discovery)):
        if operation.identity == identity:
            return operation
    raise UnsupportedOperation(f"unsupported unknown operation: {identity}")


class OperationContext:
    """Public read projections and OpCall construction, without an editing handle.

    Values returned to the contributor are detached. Python introspection is not
    confined here; untrusted Python and remote/App hosts are unsupported.
    """
    __slots__ = ("__clip", "__time", "__quantize", "fps")

    def __init__(self, tl):
        def clip(address):
            from astrid.sdk.timeline.commands import scoped_clip
            value = tl.clip(address)
            picture = tl.cut_picture(value.cut) if value.cut else None
            return MappingProxyType(dict(
                address=value.address, id=value.id, cut=value.cut,
                is_audio=value.is_audio, element=value.element, track=value.track,
                identity=scoped_clip(tl, value),
                is_picture=bool(picture and scoped_clip(tl, picture) == scoped_clip(tl, value)),
                cut_start=tl.cut(value.cut).start if value.cut else None,
                cut_end=tl.cut(value.cut).end if value.cut else None,
                start=value.start, end=value.end, moments=copy.deepcopy(value.moments),
            ))
        self.__clip = clip
        self.__time = tl.time
        self.__quantize = tl.quantize
        self.fps = tl.fps

    def clip(self, address):
        return self.__clip(address)

    def time(self, reference):
        return self.__time(reference)

    def quantize(self, seconds):
        return self.__quantize(seconds)

    @staticmethod
    def command(op, select=None, **args):
        from astrid.sdk.timeline.commands import REGISTRY
        command = EditCommand.coerce(dict(op=op, select=select, args=args))
        entry = REGISTRY.get(command.op)
        if entry is None or entry.boundary != "edit":
            raise UnsupportedOperation(f"unsupported operation command {command.op!r}")
        return command


def plan_operation(tl, operation, inputs, *, host="sdk"):
    """Validate inputs, re-admit exact code identity, then return existing OpCalls."""
    from astrid.core.pack.canonical import validate_canonical_pack
    if host != "sdk":
        raise UnsupportedOperation(f"unsupported timeline operation host: {host}")
    if not operation.eligible:
        raise UnsupportedOperation(f"unsupported execution: {operation.eligibility_reason}")
    admitted = validate_canonical_pack(operation.pack_root)
    current = next((row for row in _declarations(admitted.root, admitted.definition, admitted.resources)
                    if f"{POINT}/{row[1]['id']}" == operation.identity), None)
    if current is None or current[2] != operation.implementation_digest:
        raise CommandError("operation implementation changed; rediscover before invoking")
    try:
        inputs = json.loads(json.dumps(inputs, allow_nan=False))
        jsonschema.Draft202012Validator(current[1]["inputs"]).validate(inputs)
    except (TypeError, ValueError, jsonschema.ValidationError) as exc:
        raise CommandError(f"invalid operation inputs: {exc}") from exc
    implementation = current[1]["implementation"]
    # Execute the admitted source bytes directly; same-name neighbours and .pyc
    # caches never participate in entrypoint resolution.
    source = (admitted.root / implementation["path"]).read_bytes()
    expected = next(h.sha256 for h in admitted.resources if h.path == implementation["path"])
    if hashlib.sha256(source).hexdigest() != expected:
        raise CommandError("operation implementation changed during invocation")
    namespace = {"__name__": f"_astrid_operation_{operation.implementation_digest}",
                 "__file__": str(admitted.root / implementation["path"])}
    exec(compile(source, namespace["__file__"], "exec"), namespace)
    function = namespace.get(implementation["function"])
    if not callable(function):
        raise CommandError("declared operation function is not callable")
    values = function(OperationContext(tl), inputs)
    if not isinstance(values, (list, tuple)):
        raise CommandError("an operation must return a list of EditCommands")
    calls = tuple(EditCommand.coerce(value) for value in values)
    from astrid.sdk.timeline.commands import REGISTRY
    for call in calls:
        entry = REGISTRY.get(call.op)
        if entry is None or entry.boundary != "edit":
            raise UnsupportedOperation(f"unsupported operation command {call.op!r}")
    return calls


def operation_call(operation, inputs):
    """The local CLI/runner envelope pins the exact admitted implementation.

    Publication uses the expanded primitive calls from the returned Plan; this
    envelope is never a remote-code execution request to Runtime.
    """
    return EditCommand.coerce(dict(op="timeline.operation", select=None, args=dict(
        identity=operation.identity, implementation_digest=operation.implementation_digest, inputs=inputs)))


def invoke_operation(tl, operation, inputs, *, dry_run=False, host="sdk"):
    """One existing transaction/undo step; return the runner's existing Plan shape."""
    from astrid.sdk.timeline.commands import run
    calls = plan_operation(tl, operation, inputs, host=host)
    label = f"{operation.identity}@{operation.implementation_digest}"
    with tl.edit(label, dry_run=dry_run, quiet=True) as tx:
        if label not in tx.label:
            tx.label += "; " + label
        tx.canonical = True
        if calls:
            run(tl, dict(op="timeline.batch", select=None,
                         args={"commands": [call.to_json() for call in calls]}))
    return tx.plan
