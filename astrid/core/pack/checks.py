"""V3 manifest-backed timeline checks.

Discovery validates declared metadata and resource identities without importing
contribution code. The trusted local SDK host re-admits the closure and runs an
implementation only when the existing Check runner invokes it.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from astrid.sdk.timeline.commands import CommandError

POINT = "timeline.check"
_SCHEMA = Path(__file__).parent / "schemas" / "check.json"


class UnsupportedCheck(CommandError):
    """The requested Check identity or host is unavailable."""

    code = "unsupported"


@dataclass(frozen=True)
class CheckHostContext:
    """SDK-owned document and selected-asset reads made available to a Check."""

    host: str
    document: Any
    selected_assets: Callable[[], Iterable[Mapping[str, Any]]] | None = None


@dataclass(frozen=True)
class PackCheck:
    identity: str
    implementation_digest: str
    pack_root: Path
    descriptor_path: str
    descriptor: Mapping[str, Any]
    source_kind: str
    eligible: bool
    eligibility_reason: str

    @property
    def name(self) -> str:
        return self.identity.removeprefix(POINT + "/").replace(".", "-")

    @property
    def finding(self) -> Mapping[str, str]:
        return self.descriptor["finding"]

    def to_json(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "implementation_digest": self.implementation_digest,
            "descriptor": copy.deepcopy(dict(self.descriptor)),
            "source_kind": self.source_kind,
            "eligible": self.eligible,
            "eligibility_reason": self.eligibility_reason,
        }


def _declarations(root, definition, resources):
    handles = {item.path: item for item in resources}
    paths = definition.extensions.get("timeline", {}).get("checks", ())
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    result = []
    seen: set[str] = set()
    for path in paths:
        handle = handles.get(path)
        if handle is None or handle.file_kind != "file":
            raise ValueError(f"check descriptor {path!r} must be a declared file resource")
        payload = yaml.safe_load(handle.resolved.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)
        identity = f"{POINT}/{payload['id']}"
        if not payload["id"].startswith(definition.id + "."):
            raise ValueError(f"check {payload['id']!r} must belong to pack {definition.id!r}")
        if identity in seen:
            raise ValueError(f"duplicate check identity {identity}")
        seen.add(identity)
        code = handles.get(payload["implementation"]["path"])
        if code is None or code.file_kind != "file" or code.resolved.suffix != ".py":
            raise ValueError("check implementation must be a declared Python file resource")
        closure = {item.path: item.sha256 for item in resources if item.file_kind == "file"}
        digest = hashlib.sha256(json.dumps({
            "identity": identity,
            "declaration": payload,
            "resources": closure,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        result.append((path, payload, digest))
    return result


def validate_check_declarations(root, definition, resources):
    """Canonical admission hook; descriptor discovery never executes code."""
    from astrid.core.pack.canonical import CanonicalPackValidationError

    try:
        _declarations(root, definition, resources)
    except (ValueError, TypeError, KeyError, yaml.YAMLError, jsonschema.SchemaError,
            jsonschema.ValidationError) as exc:
        raise CanonicalPackValidationError(f"invalid timeline check: {exc}") from exc


class CheckAdmissionContext:
    """Lazy metadata census owned and closed by one outer Edit.

    Only canonical entries/resources and Check adapters live here. Documents,
    selected assets and findings always belong to the individual report.
    Invocation deliberately does not consult these retained admissions.
    """

    def __init__(self):
        self._entries = {}
        self._registry = None
        self._closed = False

    def admit(self, root):
        from astrid.core.pack.canonical import validate_canonical_pack

        self._require_open()
        key = Path(root).resolve()
        if key not in self._entries:
            self._entries[key] = validate_canonical_pack(root)
        return self._entries[key]

    def registry(self):
        from astrid.packs.rendering.actions.timeline_visualize.layers.base import checks

        self._require_open()
        if self._registry is None:
            self._registry = checks(include_v3=True, _admission_context=self)
        return self._registry

    def _require_open(self):
        if self._closed:
            raise RuntimeError("check admission context is closed")

    def close(self):
        self._entries.clear()
        self._registry = None
        self._closed = True


def discover_checks(*, project_root=None, extra_pack_roots=(), _admission_context=None):
    """Discover point-qualified checks from admitted V3 pack manifests."""
    from astrid.core.foundation.paths import REPO_ROOT
    from astrid.core.pack import discover_packs
    from astrid.core.pack.canonical import validate_canonical_pack
    from astrid.core.pack.discovery import discover_pack_metadata
    from astrid.core.pack.loader import packs_root
    from astrid.core.rendering.registry import _derive_pack_trust

    # The default project's local layer shares the bundled pack root. Admit
    # that same census once within this discovery, preserving both layers'
    # provenance. A subsequent discovery starts with no retained admissions.
    admitted_roots = {}

    def scan(root=None):
        key = Path(packs_root() if root is None else root).resolve()
        if key not in admitted_roots:
            admitted_roots[key] = discover_packs(root)
        return admitted_roots[key]

    found: dict[str, PackCheck] = {}
    for item in discover_pack_metadata(
        project_root=REPO_ROOT if project_root is None else project_root,
        extra_pack_roots=tuple(str(path) for path in extra_pack_roots),
        discover_packs_fn=scan,
    ):
        if str(item.pack.schema_version) != "3":
            continue
        if not item.pack.extensions.get("timeline", {}).get("checks"):
            continue
        admitted = (_admission_context.admit(item.pack.root) if _admission_context is not None
                    else validate_canonical_pack(item.pack.root))
        trust = _derive_pack_trust(item)
        for path, payload, digest in _declarations(admitted.root, admitted.definition, admitted.resources):
            identity = f"{POINT}/{payload['id']}"
            candidate = PackCheck(identity, digest, admitted.root, path, payload,
                                  item.source_kind, trust.eligible, trust.reason)
            if identity not in found or (not found[identity].eligible and candidate.eligible):
                found[identity] = candidate
    return tuple(found.values())


def resolve_check(identity, **discovery):
    """Resolve only a complete point-qualified identity."""
    if not isinstance(identity, str) or not identity.startswith(POINT + "/"):
        raise UnsupportedCheck(f"unsupported check identity: {identity!r}; use {POINT}/<pack>.<key>")
    for check in discover_checks(**discovery):
        if check.identity == identity:
            return check
    raise UnsupportedCheck(f"unsupported unknown check: {identity}")


class _DeclaredCheckContext:
    __slots__ = ("_check", "_host_context", "_reads")

    def __init__(self, check: PackCheck, host_context: CheckHostContext):
        self._check = check
        self._host_context = host_context
        self._reads = frozenset(check.descriptor["reads"])

    @property
    def document(self) -> Any:
        if "timeline.document" not in self._reads:
            raise UnsupportedCheck("timeline document was not declared in this check's read set")
        return _json_copy(self._host_context.document, "timeline document")

    @property
    def thresholds(self) -> Mapping[str, Any]:
        return copy.deepcopy(dict(self._check.descriptor["thresholds"]))

    def selected_assets(self) -> tuple[Mapping[str, Any], ...]:
        if "timeline.selected_assets" not in self._reads:
            raise UnsupportedCheck("selected assets were not declared in this check's read set")
        reader = self._host_context.selected_assets
        if not callable(reader):
            raise UnsupportedCheck("selected-asset Check context is unavailable")
        # Keep failures from the SDK's existing asset measurement path
        # observable (for example, a missing required pixel module). A missing
        # measurement is represented by ``None`` in the selected row instead.
        rows = list(reader())
        if any(not isinstance(row, Mapping) for row in rows):
            raise CommandError("selected-asset Check context returned a non-object row")
        return tuple(_json_copy(row, "selected asset") for row in rows)


def _json_copy(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise CommandError(f"{label} is not strict JSON: {exc}") from exc


def invoke_check(check: PackCheck, host_context: CheckHostContext | None, *, host: str = "sdk") -> list[str]:
    """Re-admit and invoke declared code after a supported host context exists."""
    if host != "sdk":
        raise UnsupportedCheck(f"unsupported timeline check host: {host}")
    if host_context is None or host_context.host != host:
        raise UnsupportedCheck("timeline Check context is unavailable for the SDK host")
    reads = set(check.descriptor["reads"])
    if "timeline.document" in reads and host_context.document is None:
        raise UnsupportedCheck("timeline document Check context is unavailable")
    if "timeline.selected_assets" in reads and not callable(host_context.selected_assets):
        raise UnsupportedCheck("selected-asset Check context is unavailable")
    if not check.eligible:
        raise UnsupportedCheck(f"unsupported execution: {check.eligibility_reason}")

    from astrid.core.pack.canonical import validate_canonical_pack

    admitted = validate_canonical_pack(check.pack_root)
    current = next((row for row in _declarations(admitted.root, admitted.definition, admitted.resources)
                    if f"{POINT}/{row[1]['id']}" == check.identity), None)
    if current is None or current[2] != check.implementation_digest:
        raise CommandError("check implementation changed; rediscover before invoking")
    descriptor = current[1]
    implementation = descriptor["implementation"]
    source = (admitted.root / implementation["path"]).read_bytes()
    expected = next(handle.sha256 for handle in admitted.resources if handle.path == implementation["path"])
    if hashlib.sha256(source).hexdigest() != expected:
        raise CommandError("check implementation changed during invocation")
    namespace = {"__name__": f"_astrid_check_{check.implementation_digest}",
                 "__file__": str(admitted.root / implementation["path"])}
    exec(compile(source, namespace["__file__"], "exec"), namespace)
    function = namespace.get(implementation["function"])
    if not callable(function):
        raise CommandError("declared check function is not callable")
    result = function(_DeclaredCheckContext(check, host_context))
    if result is None:
        return []
    if not isinstance(result, (list, tuple)) or any(not isinstance(message, str) for message in result):
        raise CommandError("declared check function must return a list of finding messages")
    return list(result)


def registered_checks(*, project_root=None, extra_pack_roots=(), _admission_context=None):
    """Adapt admitted declarations into the existing renderer Check contract."""
    from astrid.packs.rendering.actions.timeline_visualize.layers.base import Check

    registered = []
    for candidate in discover_checks(project_root=project_root, extra_pack_roots=extra_pack_roots,
                                     _admission_context=_admission_context):
        code = candidate.finding["code"]
        severity = candidate.finding["severity"]
        show_scope = candidate.finding.get("location", "scope") == "scope"

        def run(context, *, _candidate=candidate, _code=code, _severity=severity, _show_scope=show_scope):
            messages = invoke_check(_candidate, context.extension_context)
            return [context.finding(_code, message, severity=_severity, show_scope=_show_scope)
                    for message in messages]

        registered.append(Check(
            name=candidate.name,
            help=candidate.descriptor["description"],
            run=run,
            scope=candidate.descriptor["scope"],
            needs=("doc",),
            codes=(code,),
            source=f"{candidate.pack_root.name} pack V3",
            severity=severity,
            reads=tuple(candidate.descriptor["reads"]),
        ))
    return tuple(registered)
