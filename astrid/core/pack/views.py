"""Declarative read-only views in the existing canonical V3 pack closure.

No contributor callable, secondary registry or evaluator. Discovery reads declared
resources; presentation consumes one host-owned, exact-revision read model.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import jsonschema
import yaml

from astrid.sdk.timeline._common import TimelineEditError

POINT = "timeline.view"
_SCHEMA = Path(__file__).parent / "schemas/view.json"


class UnsupportedView(TimelineEditError):
    code = "unsupported"


@dataclass(frozen=True)
class PackView:
    identity: str
    implementation_digest: str
    pack_root: Path
    descriptor_path: str
    descriptor: Mapping[str, Any]
    source_kind: str
    eligible: bool
    eligibility_reason: str

    def to_json(self):
        return dict(identity=self.identity, implementation_digest=self.implementation_digest,
                    descriptor=copy.deepcopy(dict(self.descriptor)), source_kind=self.source_kind,
                    eligible=self.eligible, eligibility_reason=self.eligibility_reason)


def _declarations(root, definition, resources):
    handles = {item.path: item for item in resources}
    validator = jsonschema.Draft202012Validator(json.loads(_SCHEMA.read_text()))
    rows, seen = [], set()
    for path in definition.extensions.get("timeline", {}).get("views", ()):
        handle = handles.get(path)
        if handle is None or handle.file_kind != "file":
            raise ValueError(f"view descriptor {path!r} must be a declared file resource")
        payload = yaml.safe_load(handle.resolved.read_text(encoding="utf-8"))
        validator.validate(payload)
        jsonschema.Draft202012Validator.check_schema(payload["schema"])

        def local_refs(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"$ref", "$dynamicRef"} and (not isinstance(child, str) or not child.startswith("#")):
                        raise ValueError("view schema supports only local JSON references")
                    local_refs(child)
            elif isinstance(value, list):
                for child in value:
                    local_refs(child)
        local_refs(payload["schema"])
        if not payload["id"].startswith(definition.id + "."):
            raise ValueError("view identity must belong to its pack")
        identity = f"{POINT}/{payload['id']}"
        if identity in seen:
            raise ValueError(f"duplicate view identity {identity}")
        seen.add(identity)
        closure = {item.path: item.sha256 for item in resources if item.file_kind == "file"}
        digest = hashlib.sha256(json.dumps(dict(identity=identity, declaration=payload,
            resources=closure), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        rows.append((path, payload, digest))
    return rows


def validate_view_declarations(root, definition, resources):
    from astrid.core.pack.canonical import CanonicalPackValidationError
    try:
        _declarations(root, definition, resources)
    except (ValueError, TypeError, KeyError, yaml.YAMLError, jsonschema.SchemaError,
            jsonschema.ValidationError) as exc:
        raise CanonicalPackValidationError(f"invalid timeline view: {exc}") from exc


def discover_views(*, project_root=None, extra_pack_roots=()):
    from astrid.core.foundation.paths import REPO_ROOT
    from astrid.core.pack.canonical import validate_canonical_pack
    from astrid.core.pack.discovery import discover_pack_metadata
    from astrid.core.rendering.registry import _derive_pack_trust
    found = {}
    for item in discover_pack_metadata(project_root=REPO_ROOT if project_root is None else project_root,
                                      extra_pack_roots=tuple(str(p) for p in extra_pack_roots)):
        if str(item.pack.schema_version) != "3" or not item.pack.extensions.get("timeline", {}).get("views"):
            continue
        admitted = validate_canonical_pack(item.pack.root)
        trust = _derive_pack_trust(item)
        for path, payload, digest in _declarations(admitted.root, admitted.definition, admitted.resources):
            identity = f"{POINT}/{payload['id']}"
            candidate = PackView(identity, digest, admitted.root, path, payload,
                                 item.source_kind, trust.eligible, trust.reason)
            if identity not in found or (not found[identity].eligible and candidate.eligible):
                found[identity] = candidate
    return tuple(found.values())


def resolve_view(identity, **discovery):
    if isinstance(identity, str) and identity.startswith(POINT + "/"):
        for view in discover_views(**discovery):
            if view.identity == identity:
                return view
    raise UnsupportedView(f"unsupported view {identity!r}; use {POINT}/<pack>.<key>")


class TimelineReadModels:
    """Local host boundary, calling actual producer APIs, never pack code.

    This host opens no launcher/service. Browser execution requires an admitted
    transport supplied by its owner; it cannot fall back to this Python host.
    """
    def __init__(self, client):
        if client is None or not callable(getattr(getattr(client, "timelines", None), "open_bundle", None)):
            raise UnsupportedView("unsupported view host: exact-revision bundle reads unavailable")
        self.client = client

    def story_overview(self, *, project, timeline, revision_id):
        try:
            from astrid.sdk.timeline.story import open_story_overview
        except ImportError as exc:
            raise UnsupportedView("unsupported: story read-model producer is unavailable") from exc
        return open_story_overview(project, timeline, revision_id=revision_id, client=self.client)


def read_view(view, *, project, timeline, revision_id, host):
    """Re-admit closure and validate source binding before exposing read output."""
    from astrid.core.pack.canonical import validate_canonical_pack
    if not view.eligible or host is None:
        raise UnsupportedView(f"unsupported view host: {view.eligibility_reason if not view.eligible else 'unavailable'}")
    if not all(isinstance(v, str) and v for v in (project, timeline, revision_id)):
        raise TimelineEditError("view requires exact project, timeline and revision_id")
    admitted = validate_canonical_pack(view.pack_root)
    row = next((r for r in _declarations(admitted.root, admitted.definition, admitted.resources)
                if f"{POINT}/{r[1]['id']}" == view.identity), None)
    if row is None or row[2] != view.implementation_digest or row[1] != dict(view.descriptor):
        raise TimelineEditError("view declaration changed; rediscover before reading")
    reader = getattr(host, row[1]["read_model"], None)
    if not callable(reader) or row[1]["read_model"].startswith("_"):
        raise UnsupportedView(f"unsupported read model: {row[1]['read_model']}")
    data = reader(project=project, timeline=timeline, revision_id=revision_id)
    if not isinstance(data, Mapping) or any(data.get(k) != v for k, v in (
        ("project_id", project), ("timeline_id", timeline), ("revision_id", revision_id))):
        raise TimelineEditError("view read model project/timeline/revision mismatch")
    if not isinstance(data.get("content_digest"), str) or len(data["content_digest"]) != 64 \
            or any(c not in "0123456789abcdef" for c in data["content_digest"]):
        raise TimelineEditError("view read model omitted content digest")
    try:
        jsonschema.Draft202012Validator(row[1]["schema"]).validate(data)
    except jsonschema.ValidationError as exc:
        raise TimelineEditError(f"invalid view read model: {exc.message}") from exc
    return dict(view=view.to_json(), data=copy.deepcopy(dict(data)))


def render_view_text(result):
    """Generic recursive schema-labelled projection; no read-model field dispatch."""
    lines = [result['view']['descriptor']['label']]
    def render(value, schema, depth=0, label=None):
        indent = '  ' * depth
        if isinstance(value, Mapping):
            if label is not None:
                lines.append(f"{indent}{label}:")
                depth += 1
            for key, child in value.items():
                sub = schema.get('properties', {}).get(key, {})
                render(child, sub, depth, sub.get('title', key))
        elif isinstance(value, list):
            lines.append(f"{indent}{label or 'items'}: [{len(value)}]")
            for index, child in enumerate(value):
                render(child, schema.get('items', {}), depth + 1, str(index))
        else:
            lines.append(f"{indent}{label}: {json.dumps(value, ensure_ascii=False)}")
    render(result['data'], result['view']['descriptor']['schema'])
    return '\n'.join(lines)
