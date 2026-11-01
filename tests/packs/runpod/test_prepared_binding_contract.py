"""Fake custody through the existing in-process Runtime binding contract."""

from __future__ import annotations

import hashlib
import json
import socket
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from astrid.packs.runpod.prepared_task import PreparedWorkerError, PreparedWorkerSelection
from runtime_protocol.errors import AuthorizationError
from runtime_protocol.remote_worker_deployment import deployment_binding_from_task
from runtime_protocol.service import RuntimeService
from runtime_protocol.store import RealmStore


def test_prepared_selection_round_trips_exact_runtime_binding(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("prepared binding proof must never contact a provider")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    target = {
        "kind": "runpod",
        "provider_account_ref": "fake-account",
        "pod_id": "fake-pod",
        "storage": {"network_volume_id": "fake-volume", "name": "storage"},
        "mounts": [{"source": "fake-volume", "target": "/workspace", "read_only": False}],
    }
    claim_path = tmp_path / "claim.json"
    claim_path.write_text(json.dumps({
        "state": "claimed", "provider_account_ref": "fake-account", "pod_id": "fake-pod",
        "network_volume_id": "fake-volume", "network_volume_name": "storage",
    }), encoding="utf-8")
    selection = PreparedWorkerSelection(
        claim_path, tmp_path / "profile.json", tmp_path / "staging.json",
        tmp_path / "journal", target, {}, {},
    )
    supplied = {"schema_version": 1, "target": deepcopy(target)}
    before = deepcopy(supplied)
    request = selection.execution_request(supplied)
    assert supplied == before
    assert request == {
        **supplied, "lifecycle": {"mode": "leave_running"},
        "remote_activation_required": True,
    }
    for wrong in (
        {"provider_account_ref": "other-account"},
        {"pod_id": "other-pod"},
        {"storage": {"network_volume_id": "other-volume"}},
    ):
        with pytest.raises(PreparedWorkerError):
            selection.execution_request({**supplied, "target": {**target, **wrong}})

    root = tmp_path / "realm"
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    capability = "vibecomfy.run"
    digest = "sha256:" + hashlib.sha256(capability.encode()).hexdigest()
    try:
        service.register_capability({"capability_id": capability, "definition_digest": digest})
        service.register_executor(
            {"executor_id": "fake-host", "capabilities": [capability], "max_concurrency": 1},
            idempotency_key="fake-register",
        )
        admitted = service.create_task({
            "capability_id": capability, "capability_digest": digest,
            "input_object_ids": [], "spec": {"inputs": {"prompt": "fake workflow"}},
            "execution_request": request, "idempotency_key": "fake-admission",
        }, enforce_readiness=True)
        task_id, run_id = admitted["task"]["id"], admitted["run"]["id"]
        readback = service.task(task_id)
        assert readback["task"]["execution_request"] == request
        binding = readback["execution_binding"]
        assert binding["resolved_target"] == target
        assert binding["provider_account_ref"] == "fake-account"
        assert binding["pod_id"] == "fake-pod"
        assert binding["storage"] == target["storage"]
        assert binding["mounts"] == target["mounts"]
        assert binding["task_id"] == task_id
        assert binding["run_id"] == run_id
        assert binding["attempt_id"] is None
        assert binding["fence"] == 0
        # The workspace.v1 task RPC exposes _task_resource, rather than the
        # legacy task/run store envelope used above to inspect the binding.
        deployment = deployment_binding_from_task(service._task_resource(readback))
        assert deployment.admission_identity.task_id == task_id
        assert deployment.admission_identity.run_id == run_id
        assert deployment.placement.original_target == target
        assert deployment.placement.effective_target == target

        # Fake the owner-observed activation receipt, never credentials or a pod.
        evidence = "sha256:" + "a" * 64
        qualification = service.record_remote_activation(task_id, {
            "activation_id": "fake-activation", "task_id": task_id, "run_id": run_id,
            "credential_actor": "fake-host", "binding_digest": deployment.digest(),
            "deployment_digest": "sha256:" + "b" * 64, "evidence_digest": evidence,
            "observation_digest": "sha256:" + "c" * 64,
            "effective_target": target, "executor_incarnation": "fake-incarnation",
            "runtime_session_id": service.runtime_session_id,
            "runtime_epoch": service.health()["runtime_epoch"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "authorized_child_lineage": {
                "parent_task_id": task_id, "parent_run_id": run_id, "max_depth": 1,
                "placement_version": 0, "effective_target": target,
            },
        }, identity={"actor": "owner", "scopes": ["admin"]})
        identity = {
            "actor": "fake-host", "scopes": ["worker:execute"],
            "qualified_activation": qualification,
            "execution_binding": {
                "actual": target, "executor_incarnation": "fake-incarnation",
                "verification": {
                    "method": "credential_claim", "evidence_digest": evidence, "verified": True,
                },
            },
        }
        claim_body = {
            "executor_id": "fake-host", "capability_ids": [capability],
            "runtime_epoch": service.health()["runtime_epoch"], "target": target,
        }
        for index, wrong in enumerate((
            {"provider_account_ref": "other-account"}, {"pod_id": "other-pod"},
            {"storage": {"network_volume_id": "other-volume"}},
            {"mounts": [{"source": "fake-volume", "target": "/other", "read_only": False}]},
        )):
            foreign_identity = deepcopy(identity)
            foreign_identity["execution_binding"]["actual"].update(wrong)
            rejected = service.claim_next(
                claim_body, idempotency_key=f"fake-mismatch-{index}", identity=foreign_identity,
            )
            assert rejected["waiting_reason"] == "execution_binding_mismatch"
            assert service.task(task_id)["execution_binding"]["attempt_id"] is None

        claimed = service.claim_next(claim_body, idempotency_key="fake-claim", identity=identity)
        bound = service.task(task_id)["execution_binding"]
        assert bound == claimed["execution_binding"]
        assert bound["binding_id"] == binding["binding_id"]
        assert bound["actual_target"] == target
        assert bound["attempt_id"] == claimed["attempt_id"]
        assert bound["lease_id"] == claimed["lease_id"]
        assert bound["fence"] == claimed["fence"] > 0
        lease = {key: claimed[key] for key in ("lease_id", "fence", "runtime_epoch")}
        stale_identity = deepcopy(identity)
        stale_identity["execution_binding"]["executor_incarnation"] = "foreign-incarnation"
        with pytest.raises(AuthorizationError, match="remote activation is missing, revoked, or stale"):
            service.heartbeat_attempt(
                claimed["attempt_id"], lease, idempotency_key="fake-stale-heartbeat",
                identity=stale_identity,
            )
        assert service.heartbeat_attempt(
            claimed["attempt_id"], lease, idempotency_key="fake-heartbeat", identity=identity,
        )["data"]["attempt_id"] == claimed["attempt_id"]
    finally:
        service.close()

    # A restart preserves exact custody and fences the old active attempt.
    restarted = RuntimeService(root)
    try:
        persisted = restarted.task(task_id)
        assert persisted["task"]["execution_request"] == request
        stale = persisted["execution_binding"]
        for key in ("binding_id", "task_id", "run_id", "attempt_id", "lease_id", "fence",
                    "provider_account_ref", "pod_id", "storage", "mounts", "actual_target"):
            assert stale[key] == bound[key]
        assert stale["status"] == "stale"
        assert persisted["task"]["waiting_reason"] == "provider_state_unknown"
        assert deployment_binding_from_task(restarted._task_resource(persisted)).digest() == deployment.digest()
    finally:
        restarted.close()
