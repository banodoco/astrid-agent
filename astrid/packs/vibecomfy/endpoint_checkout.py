"""Data-only handoff from the admitted host to its action's local checkout.

Controller claim paths in endpoint evidence remain inert here. Only the
original target-local bearer sibling is resolved, by the HTTP transport.
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from astrid.core.generation.engine_contracts import (
    AttestationError, EndpointAuth, EngineEndpoint, LifecycleOwner, SecretRef,
    UnsupportedPlacement,
)
from astrid.core.generation.engine_endpoint import (
    _load, endpoint_to_dict, parse_endpoint, parse_endpoint_profile_evidence,
    read_private_endpoint_file,
)
from astrid.sdk.execution_request import normalize_execution_request

EXECUTION_REQUEST_ENV = "ASTRID_VIBECOMFY_EXECUTION_REQUEST_JSON"
ENDPOINT_ID = "vibecomfy-h3"


def _invalid() -> None:
    raise AttestationError("workflow endpoint readiness binding is invalid")


def read_host_workflow_execution_request(environ: Mapping[str, str]) -> dict | None:
    if EXECUTION_REQUEST_ENV not in environ:
        return None
    raw = environ[EXECUTION_REQUEST_ENV]
    if not isinstance(raw, str) or not raw or len(raw.encode()) > 1024 * 1024:
        _invalid()
    value = _load(raw)
    try:
        request = normalize_execution_request(value)
    except (TypeError, ValueError):
        _invalid()
    if not isinstance(request, dict) or request != value:
        _invalid()
    return request


def read_workflow_profile(profile_path: str | Path, profile_hash: str) -> dict:
    """Hash exact bytes read through protected actual opens, then parse strictly."""
    raw = read_private_endpoint_file(str(profile_path), deadline=time.monotonic() + 20,
                                     private=False)
    if not isinstance(profile_hash, str) or profile_hash != "sha256:" + hashlib.sha256(raw).hexdigest():
        _invalid()
    value = _load(raw)
    if not isinstance(value, dict):
        _invalid()
    if "workflow_endpoint" in value:
        protected = read_private_endpoint_file(str(profile_path), deadline=time.monotonic() + 20,
                                                private_directory=True)
        if protected != raw:
            _invalid()
    return value


def load_workflow_endpoint(profile: Mapping[str, Any], *, profile_path: Path,
                           execution_request: Mapping[str, Any]) -> EngineEndpoint:
    from astrid.core.generation.engine_recipe import load_recipe
    from runpod_lifecycle.https import PORT, pod_url

    try:
        request = normalize_execution_request(execution_request)
    except (TypeError, ValueError):
        _invalid()
    target = request.get("target", {}) if isinstance(request, dict) else {}
    if target.get("kind") != "runpod":
        raise UnsupportedPlacement("workflow endpoint requires the admitted RunPod target")
    if (not isinstance(profile, Mapping) or profile.get("schema_version") != "hc03-worker-readiness.v1"
            or profile.get("status") != "ready"):
        _invalid()
    path = str(profile_path)
    selected = PurePosixPath(path)
    if (not path.startswith("/") or str(selected) != path
            or any(p in ("", ".", "..") for p in path.split("/")[1:]) or "\\" in path):
        _invalid()
    slot = profile.get("workflow_endpoint")
    if type(slot) is not dict or set(slot) != {"endpoint_id", "token_ref"}:
        _invalid()
    token = slot["token_ref"]
    if (slot["endpoint_id"] != ENDPOINT_ID or type(token) is not dict
            or set(token) != {"kind", "path"} or token["kind"] != "file"
            or token["path"] != str(selected.parent / "engine-bearer.token")):
        _invalid()
    evidence = parse_endpoint_profile_evidence(profile.get("endpoint_evidence"))
    recipe = load_recipe(evidence.engine, evidence.platform)  # support before effects
    attestation = evidence.claim_attestation
    session = profile.get("vibecomfy_session")
    if (evidence.engine != ENDPOINT_ID or attestation.pod_id != target.get("pod_id")
            or attestation.provider_account_ref != target.get("provider_account_ref")
            or attestation.health.status != "healthy"
            or not isinstance(session, Mapping)
            or session.get("server_url") != f"http://127.0.0.1:{recipe.start.port}"):
        _invalid()
    endpoint = EngineEndpoint(
        "astrid.engine-endpoint.v1", ENDPOINT_ID, evidence.engine, evidence.platform, "runpod",
        pod_url(attestation.pod_id, PORT) + "/engines/" + ENDPOINT_ID,
        EndpointAuth("bearer-file", SecretRef("file", token["path"])), "ready", attestation.health,
        LifecycleOwner("attached", evidence.claim_operation_id, None, None, attestation.pod_id, False),
        attestation,
    )
    return parse_endpoint(endpoint_to_dict(endpoint))


def workflow_endpoint_network_policy(endpoint: EngineEndpoint) -> dict[str, Any]:
    endpoint = parse_endpoint(endpoint_to_dict(endpoint))
    from runpod_lifecycle.https import PORT, pod_url

    pod = endpoint.attestation.pod_id
    if endpoint.url != pod_url(pod, PORT) + "/engines/" + ENDPOINT_ID:
        _invalid()
    return {"allowed_protocols": ["dns", "tcp"],
            "allowed_destinations": ["127.0.0.1", urlsplit(endpoint.url).hostname + ":443"],
            "allow_redirects": False}


def checkout_from_profile(*, profile_path: Path, profile_hash: str,
                          execution_request: Mapping[str, Any], task_identity: str,
                          attempt_identity: str, execution_identity: str):
    for value in (task_identity, attempt_identity, execution_identity):
        if (not isinstance(value, str) or not value.strip() or value == "-"
                or any(ord(c) < 32 for c in value)):
            _invalid()
    request = read_host_workflow_execution_request({
        EXECUTION_REQUEST_ENV: json.dumps(execution_request, sort_keys=True, separators=(",", ":")),
    })
    original = read_workflow_profile(profile_path, profile_hash)
    load_workflow_endpoint(original, profile_path=profile_path, execution_request=request)

    @contextmanager
    def endpoint_checkout(*, hc03_profile: Mapping[str, Any], model_id: str,
                          template_id: str, invocation_identity: str,
                          expected_comfyui_version: str | None):
        from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter
        from astrid.core.generation.engine_transport import EngineEndpointTransport
        from runpod_lifecycle.https import PORT

        profile = read_workflow_profile(profile_path, profile_hash)
        if profile != original or profile != hc03_profile or invocation_identity != task_identity:
            _invalid()
        endpoint = load_workflow_endpoint(profile, profile_path=profile_path, execution_request=request)
        with EngineEndpointTransport(endpoint, pod_id=endpoint.attestation.pod_id, agent_port=PORT) as transport:
            adapter = CheckoutServerAdapter.from_host_session(
                hc03_profile=profile, model_id=model_id, template_id=template_id,
                invocation_identity=invocation_identity,
                expected_comfyui_version=expected_comfyui_version, endpoint_transport=transport,
            )
            yield adapter

    return endpoint_checkout
