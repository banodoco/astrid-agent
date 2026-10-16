"""Derive the private preparation binding from an accepted hosted activation.

No CLI, environment or request field supplies a preparation identity or path.
The host-only composition has a direct authenticated Runtime control peer;
the engine composition's relay peer is deliberately not Runtime authority.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import custody_broker, hosted_contract
from .storage_estimator import estimator_descriptor
from .storage_preparation_transport import (
    PreparationTransportError,
    ProcessIdentity,
    StoragePreparationConfig,
    validate_host_binding,
)


def configuration_from_activation(preparation):
    """Use only the retained selected launch, native actors and discovery."""
    from .generic_host import _ACTIVATION_RECEIPT_MODE

    host = preparation._claim_host
    request = preparation._request
    grant = preparation._activation_grant
    binding = getattr(preparation, "_hosted_binding", None)
    if (host is None or request is None or grant is None or binding is None
            or preparation.result is None or preparation._aborted
            or preparation.control_error is not None
            or preparation._retained_relay_actor is None):
        raise PreparationTransportError("activation_unavailable")
    profile = request["profile"]
    if profile.get("execution_mode") != "host_only_cpu":
        raise PreparationTransportError("runtime_identity_unavailable")
    actor = preparation._retained_relay_actor
    # Recheck the exact existing HostedLaunch/custody binding, not a new
    # authority payload. The activation descriptor was consumed by startup.
    if (binding.get("role") != "host"
            or binding.get("purpose") != "runtime-host-control/v1"
            or set(binding.get("sockets", {})) != {"control", "activation"}):
        raise PreparationTransportError("activation_unavailable")
    hosted_contract.verify_preparation(binding, request,
        custody_broker.RoleCustodyAuthority(Path(binding["scope"]), "host"), actor=actor)
    runtime_peer = ProcessIdentity(**actor.verify())
    host_peer = ProcessIdentity(**custody_broker.AuthenticatedCleanupActor.current().verify())
    owner = request["runtime_owner"]
    runtime = host._registered_runtime_state
    if (any(getattr(runtime_peer, key) != owner.get(key) for key in ("pid", "uid", "birth_id"))
            or host_peer.pid != os.getpid() or host_peer.uid != os.getuid()
            or grant.get("host") != {"pid": host_peer.pid, "birth_id": host_peer.birth_id}
            or grant.get("operation_id") != preparation.operation_id
            or grant.get("channel_id") != preparation.channel_id
            or grant.get("acceptance_mode") != _ACTIVATION_RECEIPT_MODE
            or owner.get("runtime_instance_id") != request["owner_epoch"]
            or owner.get("coordinator_epoch") != request["owner_epoch"]
            or runtime.get("runtime_instance_id") != request["owner_epoch"]
            or runtime.get("coordinator_epoch") != request["owner_epoch"]
            or runtime.get("source_epoch") != host.source_epoch
            or runtime.get("runtime_session_id") != host.runtime_state.get("runtime_session_id")):
        raise PreparationTransportError("runtime_identity_mismatch")
    launch = profile["host_launch"]
    roots = [launch["pack_root"], *(item["path"] for item in launch.get("additional_pack_roots", []))]
    if (tuple(dict.fromkeys(Path(root) for root in roots)) != host.pack_roots
            or Path(launch["boot_manifest_path"]) != host.boot_manifest_path
            or launch["boot_manifest_hash"].removeprefix("sha256:") != host.boot_manifest_hash):
        raise PreparationTransportError("source_mismatch")
    candidates = [record for record in host.capabilities.values() if record.has_request_storage_estimator]
    # J102 binds one selected capability at the fixed endpoint. No arbitrary
    # first-match selection or second endpoint is authorized by this contract.
    if len(candidates) != 1:
        raise PreparationTransportError("capability_binding_unavailable")
    record = candidates[0]
    registered = host._registered_state.get(record.id)
    if registered != {"capability_digest": record.capability_digest,
                      "source_digest": record.source_digest, "dependency_digest": record.dependency_digest}:
        raise PreparationTransportError("source_mismatch")
    descriptor = estimator_descriptor(record)
    config = StoragePreparationConfig(
        support_root=Path(profile["support_root"]),
        host_instance_id=grant["executor_incarnation"], host_peer=host_peer, runtime_peer=runtime_peer,
        boot_manifest_hash=host.boot_manifest_hash, runtime_realm_id=profile["workspace_uuid"],
        runtime_instance_id=runtime["runtime_instance_id"], runtime_session_id=runtime["runtime_session_id"],
        executor_id=host.executor_id, source_epoch=host.source_epoch,
        capability_id=record.id, capability_digest=record.capability_digest,
        source_digest="sha256:" + record.source_digest, dependency_digest="sha256:" + record.dependency_digest,
        estimator_id=descriptor["id"], estimator_version=descriptor["version"],
        estimator_implementation_digest=descriptor["implementation_digest"],
    )
    validate_host_binding(host, config)
    # Native identity may have changed while reading the admitted source.
    runtime_peer.matches(actor.verify())
    host_peer.matches(custody_broker.AuthenticatedCleanupActor.current().verify())
    return config
