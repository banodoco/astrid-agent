"""Inert endpoint evidence parsing; no lookup, I/O, or authority is conferred.

Recipe support and trusted startup port checks belong to later admission boundaries.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import get_args

from .engine_contracts import (
    AttestationError, EndpointAuth, EndpointProfileEvidence, EndpointState,
    EngineEndpoint, HealthEvidence, LifecycleOwner, LocalAttestation, PlatformKey,
    RunPodAttestation, SecretRef,
)


def _bad() -> None:
    raise AttestationError("invalid endpoint evidence")


def _obj(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        _bad()
    return value


def _str(value):
    if type(value) is not str or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        _bad()
    return value


def _enum(value, choices):
    if type(value) is not str or value not in choices:
        _bad()
    return value


def _id(value):
    if not re.fullmatch(r"[a-z][a-z0-9-]*", _str(value)):
        _bad()
    return value


def _pod(value):
    if not re.fullmatch(r"[a-z0-9]+", _str(value)):
        _bad()
    return value


def _digest(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        _bad()
    return value


def _path(value):
    # Lexical only: do not resolve or access controller or target files.
    value = _str(value)
    if value.startswith("/"):
        parts = value[1:].split("/")
        if "\\" in value or ":" in value:
            _bad()
    elif re.match(r"^[A-Za-z]:\\", value):
        parts = value[3:].split("\\")
        if "/" in value or ":" in value[2:]:
            _bad()
    else:
        _bad()
    if any(p in ("", ".", "..") for p in parts):
        _bad()
    return value


def _optional(value, fn):
    return None if value is None else fn(value)


def _health(value):
    v = _obj(value, "status checked_at object_info_sha256 required_nodes_present")
    status = _enum(v["status"], ("healthy", "unhealthy", "unknown"))
    timestamp = _str(v["checked_at"])
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if "T" not in timestamp or dt.utcoffset() != timedelta(0):
            _bad()
    except ValueError:
        _bad()
    digest = _optional(v["object_info_sha256"], _digest)
    nodes = v["required_nodes_present"]
    if type(nodes) is not bool:
        _bad()
    if status == "healthy" and (digest is None or not nodes):
        _bad()
    return HealthEvidence(status, timestamp, digest, nodes)


def _attestation(value):
    if type(value) is not dict:
        _bad()
    kind = value.get("kind")
    common = "kind install_receipt_path install_receipt_sha256 health object_info_sha256"
    if kind == "local-engine.v1":
        v = _obj(value, common)
    elif kind == "runpod-engine.v1":
        v = _obj(value, common + " provider_account_ref pod_id claim_handle_path claim_handle_sha256")
    else:
        _bad()
    health = _health(v["health"])
    digest = _digest(v["object_info_sha256"])
    if health.object_info_sha256 != digest:
        _bad()
    receipt = _path(v["install_receipt_path"])
    receipt_hash = _digest(v["install_receipt_sha256"])
    if kind == "local-engine.v1":
        return LocalAttestation(kind, receipt, receipt_hash, health, digest)
    return RunPodAttestation(kind, _str(v["provider_account_ref"]), _pod(v["pod_id"]),
                             _path(v["claim_handle_path"]), _digest(v["claim_handle_sha256"]),
                             receipt, receipt_hash, health, digest)


def _auth(value):
    v = _obj(value, "kind token")
    kind = _enum(v["kind"], ("none", "bearer-file"))
    token = v["token"]
    if kind == "none":
        if token is not None:
            _bad()
    else:
        token = _obj(token, "kind path")
        _enum(token["kind"], ("file",))
        token = SecretRef("file", _path(token["path"]))
    return EndpointAuth(kind, token)


def _owner(value):
    v = _obj(value, "kind owner_id handle_path job_id pod_id release_on_finish")
    kind = _enum(v["kind"], ("attached", "astrid-local", "astrid-runpod"))
    release = v["release_on_finish"]
    if type(release) is not bool or (kind == "attached" and release):
        _bad()
    return LifecycleOwner(kind, _str(v["owner_id"]), _optional(v["handle_path"], _path),
                          _optional(v["job_id"], _str), _optional(v["pod_id"], _pod), release)


def _url(url, placement, endpoint_id, owner, attestation):
    url = _str(url)
    if placement == "local":
        match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", url)
    else:
        match = re.fullmatch(r"https://([a-z0-9]+)-([1-9][0-9]{0,4})\.proxy\.runpod\.net/engines/" + re.escape(endpoint_id), url)
    if not match or int(match.group(1 if placement == "local" else 2)) > 65535:
        _bad()
    if placement == "runpod":
        pod = match.group(1)
        if (owner.pod_id is not None and owner.pod_id != pod) or (attestation is not None and attestation.pod_id != pod):
            _bad()
    return url


def parse_endpoint(value) -> EngineEndpoint:
    """Validate a closed JSON-shaped endpoint object without doing I/O."""
    v = _obj(value, "schema endpoint_id engine platform placement url auth state health lifecycle_owner attestation")
    _enum(v["schema"], ("astrid.engine-endpoint.v1",))
    endpoint_id, engine = _id(v["endpoint_id"]), _id(v["engine"])
    platform = _enum(v["platform"], get_args(PlatformKey))
    placement = _enum(v["placement"], ("local", "runpod"))
    state = _enum(v["state"], get_args(EndpointState))
    auth, owner = _auth(v["auth"]), _owner(v["lifecycle_owner"])
    health = _optional(v["health"], _health)
    attestation = _optional(v["attestation"], _attestation)
    if placement == "local":
        if auth.kind != "none" or owner.kind == "astrid-runpod" or owner.pod_id is not None or isinstance(attestation, RunPodAttestation):
            _bad()
    else:
        if platform.startswith("windows-") or owner.kind == "astrid-local" or isinstance(attestation, LocalAttestation):
            _bad()
        if owner.pod_id is not None and attestation is not None and owner.pod_id != attestation.pod_id:
            _bad()
    if health is not None and attestation is not None and health != attestation.health:
        _bad()
    if state == "ready":
        if (health is None or health.status != "healthy" or attestation is None
                or health != attestation.health or (placement == "runpod" and auth.kind != "bearer-file")):
            _bad()
    url = _url(v["url"], placement, endpoint_id, owner, attestation)
    return EngineEndpoint(v["schema"], endpoint_id, engine, platform, placement, url, auth, state, health, owner, attestation)


def endpoint_to_dict(endpoint: EngineEndpoint) -> dict:
    if type(endpoint) is not EngineEndpoint:
        _bad()
    value = asdict(endpoint)
    if parse_endpoint(value) != endpoint:
        _bad()
    return value


def parse_endpoint_profile_evidence(value) -> EndpointProfileEvidence:
    v = _obj(value, "schema engine platform claim_operation_id claim_attestation")
    _enum(v["schema"], ("astrid.runpod.endpoint-evidence.v1",))
    engine = _id(v["engine"])
    platform = _enum(v["platform"], get_args(PlatformKey))
    if platform.startswith("windows-"):
        _bad()
    attestation = _attestation(v["claim_attestation"])
    if not isinstance(attestation, RunPodAttestation):
        _bad()
    return EndpointProfileEvidence(v["schema"], engine, platform, _str(v["claim_operation_id"]), attestation)


def endpoint_profile_evidence_to_dict(evidence: EndpointProfileEvidence) -> dict:
    if type(evidence) is not EndpointProfileEvidence:
        _bad()
    value = asdict(evidence)
    if parse_endpoint_profile_evidence(value) != evidence:
        _bad()
    return value


def _load(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                _bad()
            result[key] = item
        return result
    if type(value) not in (str, bytes):
        _bad()
    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=lambda _: _bad())
    except (ValueError, UnicodeError, RecursionError):
        _bad()


def load_endpoint(value: str | bytes) -> EngineEndpoint:
    return parse_endpoint(_load(value))


def load_endpoint_profile_evidence(value: str | bytes) -> EndpointProfileEvidence:
    return parse_endpoint_profile_evidence(_load(value))
