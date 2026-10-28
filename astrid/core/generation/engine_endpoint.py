"""Endpoint evidence parsing and attachment under the original prepared owner.

The parsers remain inert: no lookup, I/O, or authority is conferred by parsing.
The explicit ensure boundary admits recipe support and the original claim.
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


def read_private_endpoint_file(path: str, *, deadline: float, private: bool = True,
                               private_directory: bool = False) -> bytes:
    """Read an owned, bounded regular file through no-follow directory fds.

    Used for the controller's immutable claim snapshot. The trusted remote
    readiness/receipt reader must supply these same actual-open guarantees.
    """
    import os
    import stat
    import time
    from .engine_contracts import DeadlineExceeded

    _path(path)
    if not path.startswith("/"):
        _bad()
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        parts = path.split("/")[1:]
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        parent_info = os.fstat(directory)
        if private_directory and (parent_info.st_uid != os.getuid()
                                  or stat.S_IMODE(parent_info.st_mode) != 0o700):
            _bad()
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or (stat.S_IMODE(info.st_mode) != 0o600 if private else bool(info.st_mode & 0o022))
                    or info.st_size > 8 * 1024 * 1024):
                _bad()
            blocks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise DeadlineExceeded("endpoint evidence deadline exceeded")
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > 8 * 1024 * 1024:
                    _bad()
                blocks.append(block)
            after = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                _bad()
            return b"".join(blocks)
        finally:
            os.close(fd)
    except OSError:
        raise AttestationError("endpoint evidence file is unavailable or unsafe") from None
    finally:
        os.close(directory)


def _prepared_session_evidence(reference, session_selection, session_receipt, profile,
                               *, engine, platform, read_file, deadline):
    import hashlib
    from .engine_recipe import load_recipe
    from .engine_runner import _parse_receipt, plan_install

    recipe = load_recipe(engine, platform)
    if (type(profile) is not dict or profile.get("schema_version") != "hc03-worker-readiness.v1"
            or profile.get("status") != "ready"):
        _bad()
    evidence = parse_endpoint_profile_evidence(profile.get("endpoint_evidence"))
    attestation = evidence.claim_attestation
    if (evidence.engine != engine or evidence.platform != platform
            or attestation.health.status != "healthy"):
        _bad()
    snapshot_bytes = read_private_endpoint_file(attestation.claim_handle_path, deadline=deadline)
    if hashlib.sha256(snapshot_bytes).hexdigest() != attestation.claim_handle_sha256:
        _bad()
    snapshot = _load(snapshot_bytes)
    session = session_receipt
    session_profile = profile.get("vibecomfy_session")
    if type(session_profile) is not dict:
        _bad()
    if (session is None or session.url != f"http://127.0.0.1:{recipe.start.port}"
            or session.config_digest != reference.session_config_digest
            or session.session_ref != reference.session_ref
            or session_profile.get("server_url") != session.url):
        _bad()
    receipt_bytes = read_file(attestation.install_receipt_path, deadline=deadline)
    if (type(receipt_bytes) is not bytes or len(receipt_bytes) > 8 * 1024 * 1024
            or hashlib.sha256(receipt_bytes).hexdigest() != attestation.install_receipt_sha256):
        _bad()
    raw_receipt = _load(receipt_bytes)
    if type(raw_receipt) is not dict:
        _bad()
    root = _path(raw_receipt.get("root"))
    if attestation.install_receipt_path != root + "/.astrid-engine/install.json":
        _bad()
    # Self-start uses the same local installer *on the pod*. Preserve its bytes
    # and placement; never rewrite a local install receipt as a RunPod claim.
    receipt = _parse_receipt(raw_receipt, plan_install(recipe, root=root, placement="local"))
    if (receipt.status != "installed" or receipt.health != attestation.health
            or session_selection is None
            or session_selection.runtime_root != root + "/runtime"):
        _bad()
    return recipe, evidence, snapshot


def _endpoint_for_prepared_claim(preparer, reference, session, evidence, snapshot, claim,
                                 *, engine, platform, endpoint_id, agent_port, token_ref):
    attestation = evidence.claim_attestation
    identity = ("operation_id", "provider_account_ref", "pod_id", "network_volume_id",
                "network_volume_name", "network_volume_size_gb",
                "network_volume_datacenter_id", "volume_mount_path", "acquisition_mode",
                "transport", "https_token_file")
    if (type(snapshot) is not dict or snapshot.get("state") != "claimed"
            or any(claim.get(k) is None or claim.get(k) != snapshot.get(k) for k in identity)
            or claim["operation_id"] != evidence.claim_operation_id
            or claim["pod_id"] != attestation.pod_id
            or claim["provider_account_ref"] != attestation.provider_account_ref
            or session.pod_id != claim["pod_id"] or session.account_ref != claim["provider_account_ref"]):
        _bad()
    target = reference.effective_target
    storage = target.get("storage", {})
    if (target.get("kind") != "runpod" or target.get("pod_id") != claim["pod_id"]
            or target.get("provider_account_ref") != claim["provider_account_ref"]
            or storage.get("network_volume_id") != claim["network_volume_id"]):
        _bad()
    attached = claim.get("acquisition_mode") == "attach"
    if claim.get("acquisition_mode") not in {"attach", "allocate"}:
        _bad()
    from runpod_lifecycle.https import PORT, pod_url
    # The existing prepared HTTPS composition always uses lifecycle's
    # agent port and original claim bearer; no caller-selected auth slot.
    if (claim["transport"] != "https" or agent_port != PORT
            or token_ref != SecretRef("file", claim["https_token_file"])):
        _bad()
    endpoint = EngineEndpoint(
        "astrid.engine-endpoint.v1", endpoint_id, engine, platform, "runpod",
        pod_url(claim["pod_id"], agent_port) + "/engines/" + endpoint_id,
        EndpointAuth("bearer-file", token_ref), "ready", attestation.health,
        # Existing prepared requests retain custody; finish/release remains
        # the original prepared owner, never this transport adapter.
        LifecycleOwner("attached" if attached else "astrid-runpod", claim["operation_id"],
                       str(preparer.claim_handle_path), None, claim["pod_id"], False), attestation)
    endpoint = parse_endpoint(endpoint_to_dict(endpoint))
    return endpoint


def _probe_prepared_endpoint(endpoint, recipe, *, agent_port, deadline):
    import hashlib
    import time
    from .engine_contracts import DeadlineExceeded, EndpointHealthError
    from .engine_transport import EngineEndpointTransport

    transport = EngineEndpointTransport(endpoint, pod_id=endpoint.attestation.pod_id, agent_port=agent_port)
    try:
        objects = transport.object_info(deadline=deadline)
    finally:
        transport.close()
    digest = hashlib.sha256(json.dumps(objects, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if digest != endpoint.attestation.object_info_sha256 or not all(node in objects for node in recipe.health.required_nodes):
        raise EndpointHealthError("claimed engine object_info differs from readiness")
    if time.monotonic() >= deadline:
        raise DeadlineExceeded("endpoint ensure deadline exceeded")


def ensure_prepared_endpoint(inspector, handle, *, engine, platform, endpoint_id,
                             agent_port, token_ref, read_file, deadline):
    """Observe one ready slot under the original prepared owner; never install.

    ``read_file(path, deadline=...)`` is the trusted target file boundary for
    the already selected readiness document and canonical install receipt.
    It returns exact bytes with bounded/no-follow reads, not asserted hashes.
    No Runtime admission, activation, source or publication check is replaced.
    """
    import hashlib
    import time
    from .engine_contracts import (
        DeadlineExceeded, EndpointOwnershipError, UnsupportedPlatform,
    )
    from .engine_recipe import load_recipe

    if not isinstance(platform, str) or not platform.startswith("linux-"):
        raise UnsupportedPlatform("prepared RunPod endpoints require Linux")
    recipe = load_recipe(engine, platform)  # support admission before effects
    if type(agent_port) is not int or not 1 <= agent_port <= 65535:
        _bad()
    _id(endpoint_id)
    if time.monotonic() >= deadline:
        raise DeadlineExceeded("endpoint ensure deadline exceeded")
    preparer = inspector.preparer
    handle = preparer._assert_handle(handle)
    reference = handle.reference
    if not reference.readiness_profile_path or not reference.readiness_profile_hash:
        _bad()
    # Keep all existing provider/process/Runtime/credential/source checks.
    inspector.observe_ready(handle)
    profile_bytes = read_file(str(reference.readiness_profile_path), deadline=deadline)
    if (type(profile_bytes) is not bytes or len(profile_bytes) > 8 * 1024 * 1024
            or "sha256:" + hashlib.sha256(profile_bytes).hexdigest() != reference.readiness_profile_hash):
        _bad()
    profile = _load(profile_bytes)
    recipe, evidence, snapshot = _prepared_session_evidence(
        reference, handle.session_selection, handle.session_receipt, profile,
        engine=engine, platform=platform, read_file=read_file, deadline=deadline,
    )
    session = handle.session_receipt
    with preparer._claim_owner() as (claim, lock_fd):
        endpoint = _endpoint_for_prepared_claim(
            preparer, reference, session, evidence, snapshot, claim,
            engine=engine, platform=platform, endpoint_id=endpoint_id,
            agent_port=agent_port, token_ref=token_ref,
        )
        current = claim.get("managed_worker")
        if (not isinstance(current, dict) or current.get("deployment_digest") != reference.digest()
                or current.get("process_handle") != handle.process.as_dict()
                or current.get("activation_operation_id") != handle.operation_id
                or current.get("activation_channel_id") != handle.channel_id
                or current.get("activation_qualification") != handle.persisted.get("activation_qualification")
                or current.get("activation_qualification", {}).get("activation_id") != handle.activation_id
                or current.get("state") not in {"committed", "ready", "ready_observed_by_preparer"}):
            raise EndpointOwnershipError("original prepared activation is no longer ready")
        previous = current.get("engine_endpoint")
        if previous is not None:
            if parse_endpoint(previous) != endpoint:
                raise EndpointOwnershipError("the original prepared endpoint slot is already bound")
            return endpoint
        _probe_prepared_endpoint(endpoint, recipe, agent_port=agent_port, deadline=deadline)
        current = {**current, "engine_endpoint": endpoint_to_dict(endpoint)}
        preparer._write_worker_state(current, owner_lock_fd=lock_fd)
        handle.persisted = current
        return endpoint


def ensure_prepared_session_endpoint(
    preparer, *, reference, staging, session_selection, session_receipt,
    profile, endpoint_id, agent_port, token_ref, read_file, deadline,
):
    """Ensure the installed session before a host/profile generation is frozen.

    This boundary never observes a nonexistent host as ready. Its result is
    retained by the original preparer until the ordinary managed-worker record
    exists; no second persistent endpoint registry or claim lock is created.
    """
    import time
    from .engine_contracts import DeadlineExceeded, EndpointOwnershipError
    from .engine_recipe import load_recipe

    evidence = parse_endpoint_profile_evidence(profile.get("endpoint_evidence"))
    recipe = load_recipe(evidence.engine, evidence.platform)  # before I/O
    _id(endpoint_id)
    if time.monotonic() >= deadline:
        raise DeadlineExceeded("endpoint ensure deadline exceeded")
    cpu = staging.get("cpu_readiness", {})
    paths = cpu.get("effective_paths", {})
    if (staging.get("status") != "cpu_ready" or staging.get("gpu_qualified") is not False
            or cpu.get("status") != "cpu_ready" or cpu.get("gpu_qualified") is not False
            or session_selection is None or session_receipt is None
            or session_selection.session_ref != reference.session_ref
            or session_selection.config_digest != reference.session_config_digest
            or session_selection.account_ref != session_receipt.account_ref
            or session_selection.pod_id != session_receipt.pod_id
            or session_selection.port != recipe.start.port
            or paths.get("model_root") != str(reference.model_root)
            or paths.get("output_root") != str(reference.output_root)
            or session_selection.output_root != str(reference.output_root)):
        _bad()
    recipe, evidence, snapshot = _prepared_session_evidence(
        reference, session_selection, session_receipt, profile,
        engine=evidence.engine, platform=evidence.platform, read_file=read_file, deadline=deadline,
    )
    # The selected session owner supplies live custody observation; registry
    # reads alone never permit process adoption or a legacy start fallback.
    preparer.session_owner.resume(session_selection, session_receipt)
    with preparer._claim_owner() as (claim, _fd):
        current = claim.get("managed_worker")
        if current is not None and current.get("state") not in {"stopped", "released"}:
            raise EndpointOwnershipError("original prepared endpoint owner changed")
        endpoint = _endpoint_for_prepared_claim(
            preparer, reference, session_receipt, evidence, snapshot, claim,
            engine=evidence.engine, platform=evidence.platform, endpoint_id=endpoint_id,
            agent_port=agent_port, token_ref=token_ref,
        )
        _probe_prepared_endpoint(endpoint, recipe, agent_port=agent_port, deadline=deadline)
        return endpoint
