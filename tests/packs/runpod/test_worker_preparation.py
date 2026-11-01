from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from astrid.packs.runpod.worker_preparation import (
    AcquisitionUnresolvedError,
    RunPodTargetContract,
    TargetObservationError,
    _release_exact_target_after_quiescence,
    acquire_target,
    inspect_target_records,
    observe_exact_target,
    resolve_existing_volume,
    write_target_record,
)
from tests.helpers.runtime import use_runtime


def _contract(**overrides: Any) -> RunPodTargetContract:
    values: dict[str, Any] = {
        "pod_id": "pod-exact",
        "expected_pod_name": "astrid-prep-1",
        "gpu_type": "NVIDIA GeForce RTX 5090",
        "worker_image": "runpod/base:cuda1300",
        "network_volume_id": "vol-persist",
        "network_volume_name": "astrid-release",
        "network_volume_size_gb": 250,
        "datacenter_id": "EU-RO-1",
    }
    values.update(overrides)
    return RunPodTargetContract(**values)


def _pod(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "pod-exact",
        "name": "astrid-prep-1",
        "desiredStatus": "RUNNING",
        "actualStatus": "RUNNING",
        "machineType": "NVIDIA GeForce RTX 5090",
        "imageName": "runpod/base:cuda1300",
        "networkVolumeId": "vol-persist",
        **overrides,
    }


def _volume(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "vol-persist",
        "name": "astrid-release",
        "size": 250,
        "dataCenterId": "EU-RO-1",
        **overrides,
    }


def _claim_handle(**overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": "astrid.runpod.claim.v1",
        "state": "claimed",
        "operation_id": "operation-1",
        "request_name": "astrid-prep-1",
        "pod_id": "pod-exact",
        "name": "astrid-prep-1",
        "gpu_type": "NVIDIA GeForce RTX 5090",
        "worker_image": "runpod/base:cuda1300",
        "network_volume_id": "vol-persist",
        "network_volume_name": "astrid-release",
        "network_volume_size_gb": 250,
        "provider_account_ref": "runpod-test-account",
        "release_authorized": True,
        "acquisition_mode": "allocate",
        **overrides,
    }


def _quiescence(record: dict[str, Any]) -> dict[str, Any]:
    task_id, activation_id = "task-exact", "activation-exact"
    return {
        "schema": "astrid.runpod.worker-quiescence.v1",
        "operation_id": record["operation_id"],
        "pod_id": record["pod_id"],
        "provider_account_ref": record["provider_account_ref"],
        "task_id": task_id,
        "activation_id": activation_id,
        "runtime_instance_id": "runtime-exact",
        "runtime_session_id": "session-exact",
        "runtime_epoch": 3,
        "drain": {"state": "complete", "task_id": task_id, "activation_id": activation_id},
        "process_stop": {
            "stopped": True,
            "pid": 4321,
            "birth_id": "birth-exact",
            "incarnation": "incarnation-exact",
        },
    }


def _run_release(
    handle_path: Path,
    handle: dict[str, Any],
    *,
    quiesce_worker: Any,
    resume: bool = False,
) -> dict[str, Any]:
    write_target_record(handle_path, handle)
    return asyncio.run(
        _release_exact_target_after_quiescence(
            SimpleNamespace(api_key="test-key"),
            handle_path=handle_path,
            attempt=handle,
            quiesce_worker=quiesce_worker,
            resume=resume,
            verify_timeout_seconds=0,
        )
    )


def test_exact_account_pod_image_gpu_and_volume_are_proved() -> None:
    observed = inspect_target_records(
        _contract(), pods=[_pod()], volumes=[_volume()]
    )

    assert observed.pod_id == "pod-exact"
    assert observed.pod_name == "astrid-prep-1"
    assert observed.gpu_type == "NVIDIA GeForce RTX 5090"
    assert observed.worker_image == "runpod/base:cuda1300"
    assert observed.network_volume_id == "vol-persist"
    assert observed.network_volume_size_gb == 250


@pytest.mark.integration  # the owner imports runtime_protocol; needs a Runtime checkout (ASTRID_RUNTIME_CHECKOUT)
def test_runtime_activation_checkpoints_restore_only_the_exact_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_runtime()
    from astrid.packs.runpod.worker_owner import (
        PreparedRunPodHost,
        RunPodRemoteWorkerPreparer,
    )
    from astrid.packs.runpod.worker_process import (
        OWNERSHIP_ROOT,
        WorkerBinding,
        WorkerProcessHandle,
    )
    from astrid.packs.runpod.worker_staging import _canonical_digest

    target = {
        "kind": "runpod",
        "pod_id": "pod-exact",
        "provider_account_ref": "account-1",
    }
    binding = WorkerBinding(
        account_ref="account-1",
        pod_id="pod-exact",
        target=target,
        runtime_instance_id="runtime-instance-1",
        runtime_epoch=4,
        runtime_session_id="runtime-session-1",
        executor_id="astrid-pack-host",
    )
    owner_root = Path(OWNERSHIP_ROOT)
    process = WorkerProcessHandle(
        binding=binding,
        incarnation="host-incarnation-1",
        marker_path=str(owner_root / f"{binding.physical_key}.json"),
        lock_path=str(owner_root / f"{binding.physical_key}.lock"),
        pid=4201,
        birth_id="boot:81",
        pgid=4201,
        sid=4201,
        lock_fd=7,
    )
    observation = {
        "target": target,
        "provider_identity": {"account_ref": "account-1", "pod_id": "pod-exact"},
        "process": {"pid": 4201, "birth_id": "boot:81", "pgid": 4201, "sid": 4201},
        "runtime_instance_id": "runtime-instance-1",
        "runtime_epoch": 4,
        "runtime_session_id": "runtime-session-1",
        "source_closure_digest": "sha256:" + "1" * 64,
        "dependency_closure_digest": "sha256:" + "2" * 64,
        "model_root": "/mnt/volume/models",
        "session_ref": "session-1",
        "data_root": "/mnt/volume/data",
        "support_root": "/mnt/volume/data/runtime",
        "output_root": "/mnt/volume/output",
        "capacity": 1,
        "model_inventory_digest": "sha256:" + "3" * 64,
        "session_config_digest": "sha256:" + "4" * 64,
    }
    evidence_digest = _canonical_digest(observation)
    deployment_digest = "sha256:" + "5" * 64
    claim_path = tmp_path / "claim.json"
    write_target_record(claim_path, _claim_handle(provider_account_ref="account-1"))
    managed = {
        "schema": "astrid.runpod.managed-worker.v1",
        "state": "parked_process",
        "claim_operation_id": "operation-1",
        "pod_id": "pod-exact",
        "provider_account_ref": "account-1",
        "deployment_digest": deployment_digest,
        "process_handle": process.as_dict(),
        "activation_operation_id": "operation-remote-1",
        "activation_channel_id": "channel-remote-1",
        "activation_socket_path": "/tmp/.a12345678/s",
        "ready_file": "/mnt/volume/data/runtime/ready.json",
        "staging_plan_digest": "sha256:" + "6" * 64,
        "gpu_qualified": False,
    }
    attempt = _claim_handle(provider_account_ref="account-1", managed_worker=managed)
    write_target_record(claim_path, attempt)
    reference = SimpleNamespace(
        effective_target=target,
        runtime_instance_id="runtime-instance-1",
        runtime_epoch=4,
        executor_id="astrid-pack-host",
        digest=lambda: deployment_digest,
        executable=SimpleNamespace(path=Path("/mnt/volume/release/python")),
        source_checkout=Path("/mnt/volume/source/astrid"),
    )
    transport = object()
    preparer = RunPodRemoteWorkerPreparer(
        SimpleNamespace(id="pod-exact"),
        provider_account_ref="account-1",
        runtime_session_id="runtime-session-1",
        claim_handle_path=claim_path,
        journal_dir=tmp_path / "journal",
        staging_inputs={},
        process_transport=transport,
    )
    host = PreparedRunPodHost(
        reference=reference,
        owner=SimpleNamespace(),
        process=process,
        operation_id="operation-remote-1",
        channel_id="channel-remote-1",
        socket_path="/tmp/.a12345678/s",
        staging={"plan_digest": managed["staging_plan_digest"]},
        ready_file=managed["ready_file"],
        persisted=managed,
    )
    preparer._handle = host
    parked = SimpleNamespace(
        handle=host,
        target=target,
        observation=observation,
        evidence_digest=evidence_digest,
        executor_incarnation=process.incarnation,
    )
    preparer.checkpoint(parked, "parked", {"schema_version": 1})
    qualification = {
        "activation_id": "activation-1",
        "task_id": "task-1",
        "run_id": "run-1",
        "executor_incarnation": process.incarnation,
        "evidence_digest": evidence_digest,
        "observation_digest": evidence_digest,
        "deployment_digest": deployment_digest,
    }
    preparer.checkpoint(parked, "qualification", {"schema_version": 1, "qualification": qualification})
    credential = tmp_path / "credential" / "executor.token"
    for stage, extra in (
        ("credential", {"credential_file": str(credential)}),
        ("acknowledged", {
            "credential_file": str(credential),
            "acknowledgement": {
                "activation_id": "activation-1",
                "executor_incarnation": process.incarnation,
                "evidence_digest": evidence_digest,
            },
        }),
        ("enabled", {"credential_file": str(credential)}),
        ("ready", {"credential_file": str(credential), "ready_observation": observation}),
        ("committed", {}),
    ):
        preparer.checkpoint(parked, stage, {"schema_version": 1, "qualification": qualification, **extra})

    class RestoringTransport:
        async def call(self, operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
            if operation == "reconcile":
                return {"handle": process.as_dict()}
            if operation == "observe":
                return {
                    "target": target,
                    "provider_identity": observation["provider_identity"],
                    "process": observation["process"],
                }
            raise AssertionError(operation)

    restored = RunPodRemoteWorkerPreparer(
        SimpleNamespace(id="pod-exact"),
        provider_account_ref="account-1",
        runtime_session_id="runtime-session-1",
        claim_handle_path=claim_path,
        journal_dir=tmp_path / "journal",
        staging_inputs={},
        process_transport=RestoringTransport(),
    )
    monkeypatch.setattr(restored, "_stage", lambda _reference: {"plan_digest": managed["staging_plan_digest"]})
    checkpoint = restored.restore(reference)
    assert checkpoint.handle.incarnation == process.incarnation
    assert checkpoint.target == target
    assert checkpoint.observation == observation
    assert checkpoint.evidence_digest == evidence_digest
    assert checkpoint.executor_incarnation == process.incarnation
    assert json.loads(claim_path.read_text(encoding="utf-8"))["managed_worker"]["state"] == "committed"


def test_remote_inspector_assembles_current_provider_process_runtime_and_model_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astrid.packs.runpod.worker_owner import (
        PreparedRunPodHost,
        RunPodRemoteWorkerInspector,
        RunPodRemoteWorkerPreparer,
    )
    from astrid.packs.runpod.worker_process import (
        OWNERSHIP_ROOT,
        WorkerBinding,
        WorkerProcessHandle,
    )
    from astrid.packs.runpod.worker_staging import _canonical_digest

    target = {
        "kind": "runpod",
        "pod_id": "pod-exact",
        "provider_account_ref": "account-1",
    }
    provider = {"account_ref": "account-1", "pod_id": "pod-exact"}
    process_fact = {"pid": 4202, "birth_id": "boot:82", "pgid": 4202, "sid": 4202}
    binding = WorkerBinding(
        account_ref="account-1",
        pod_id="pod-exact",
        target=target,
        runtime_instance_id="runtime-instance-1",
        runtime_epoch=4,
        runtime_session_id="runtime-session-1",
        executor_id="astrid-pack-host",
    )
    root = Path(OWNERSHIP_ROOT)
    process = WorkerProcessHandle(
        binding=binding,
        incarnation="host-incarnation-2",
        marker_path=str(root / f"{binding.physical_key}.json"),
        lock_path=str(root / f"{binding.physical_key}.lock"),
        pid=4202,
        birth_id="boot:82",
        pgid=4202,
        sid=4202,
        lock_fd=8,
    )
    model_root = Path("/mnt/volume/models")
    output_root = Path("/mnt/volume/output")
    dependency = SimpleNamespace(name="astrid", path=Path("/mnt/volume/source/astrid"), digest="sha256:" + "1" * 64)
    reference = SimpleNamespace(
        effective_target=target,
        runtime_instance_id="runtime-instance-1",
        runtime_epoch=4,
        executor_id="astrid-pack-host",
        runtime_endpoint="http://127.0.0.1:8080",
        source_closure_digest="sha256:" + "2" * 64,
        dependency_closure=(dependency,),
        model_root=model_root,
        session_ref="session-1",
        data_root=Path("/mnt/volume/data"),
        support_root=Path("/mnt/volume/data/runtime"),
        output_root=output_root,
        capacity=1,
        session_config_digest="sha256:" + "3" * 64,
        source_checkout=Path("/mnt/volume/source/astrid"),
        pack_roots=(Path("/mnt/volume/source/astrid/packs"),),
    )
    model_digest = "sha256:" + "4" * 64
    stage = {
        "plan_digest": "sha256:" + "5" * 64,
        "cpu_readiness": {
            "status": "cpu_ready",
            "gpu_qualified": False,
            "effective_paths": {
                "model_root": str(model_root),
                "output_root": str(output_root),
            },
            "model_root": {"path": str(model_root), "inventory_digest": model_digest},
        },
    }
    claim_path = tmp_path / "claim.json"
    write_target_record(claim_path, _claim_handle(provider_account_ref="account-1"))
    preparer = RunPodRemoteWorkerPreparer(
        SimpleNamespace(id="pod-exact"),
        provider_account_ref="account-1",
        runtime_session_id="runtime-session-1",
        claim_handle_path=claim_path,
        journal_dir=tmp_path / "journal",
        staging_inputs={"readiness_profile": {"launch": {"model_root": {"inventory_digest": model_digest}}}},
        process_transport=object(),
    )

    async def observe(_process: Any) -> Mapping[str, Any]:
        return {"target": target, "provider_identity": provider, "process": process_fact,
                "incarnation": process.incarnation}

    host = PreparedRunPodHost(
        reference=reference,
        owner=SimpleNamespace(observe=observe),
        process=process,
        operation_id="operation-remote-2",
        channel_id="channel-remote-2",
        socket_path="/tmp/.a12345678/s",
        staging=stage,
        ready_file="/mnt/volume/data/runtime/ready.json",
    )
    preparer._handle = host
    monkeypatch.setattr(preparer, "observe_provider_identity", lambda _handle: provider)
    monkeypatch.setattr(preparer, "observe_staging", lambda _handle: stage)
    inspector = RunPodRemoteWorkerInspector(
        preparer,
        runtime_observer=lambda: {
            "runtime_instance_id": "runtime-instance-1",
            "runtime_epoch": 4,
            "runtime_session_id": "runtime-session-1",
        },
        session_config_observer=lambda _handle: {
            "session_ref": "session-1",
            "session_config_digest": reference.session_config_digest,
        },
        executor_observer=lambda _executor_id: {},
    )
    observation = inspector.observe(host)
    assert observation == {
        "target": target,
        "provider_identity": provider,
        "process": process_fact,
        "runtime_instance_id": "runtime-instance-1",
        "runtime_epoch": 4,
        "runtime_session_id": "runtime-session-1",
        "source_closure_digest": reference.source_closure_digest,
        "dependency_closure_digest": _canonical_digest([
            {"name": dependency.name, "path": str(dependency.path), "digest": dependency.digest}
        ]),
        "model_root": str(model_root),
        "session_ref": "session-1",
        "data_root": str(reference.data_root),
        "support_root": str(reference.support_root),
        "output_root": str(output_root),
        "capacity": 1,
        "model_inventory_digest": model_digest,
        "session_config_digest": reference.session_config_digest,
    }


@pytest.mark.parametrize(
    ("contract_changes", "pod_changes", "volume_changes", "message"),
    [
        ({}, {"id": "pod-other"}, {}, "not uniquely visible"),
        ({}, {"name": "other"}, {}, "name mismatch"),
        ({}, {"machineType": "RTX 4090"}, {}, "GPU mismatch"),
        ({}, {"imageName": "other:image"}, {}, "image mismatch"),
        ({}, {"networkVolumeId": "vol-other"}, {}, "volume mismatch"),
        ({}, {}, {"size": 500}, "size mismatch"),
        ({}, {}, {"size": 250.5}, "size mismatch"),
        ({"datacenter_id": "EU-RO-2"}, {}, {}, "datacenter mismatch"),
        ({}, {"actualStatus": "STOPPED"}, {}, "not currently attachable"),
    ],
)
def test_mismatching_target_is_rejected_before_any_use(
    contract_changes: dict[str, Any],
    pod_changes: dict[str, Any],
    volume_changes: dict[str, Any],
    message: str,
) -> None:
    contract = _contract(**contract_changes)
    pod = _pod(**pod_changes)
    if pod_changes.get("id") == "pod-other":
        pods = [_pod(id="pod-other")]
    else:
        pods = [pod]

    with pytest.raises(TargetObservationError, match=message):
        inspect_target_records(
            contract,
            pods=pods,
            volumes=[_volume(**volume_changes)],
        )


def test_ambiguous_pod_or_volume_inventory_is_not_adopted() -> None:
    with pytest.raises(TargetObservationError, match="not uniquely visible"):
        inspect_target_records(_contract(), pods=[_pod(), _pod()], volumes=[_volume()])
    with pytest.raises(TargetObservationError, match="cannot be confirmed"):
        inspect_target_records(_contract(), pods=[_pod()], volumes=[])


def test_provider_lookup_errors_remain_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api

    def unavailable(_api_key: str) -> list[dict[str, Any]]:
        raise TimeoutError("inventory timeout")

    monkeypatch.setattr(api, "list_pods", unavailable)
    with pytest.raises(TargetObservationError, match="could not observe"):
        asyncio.run(observe_exact_target("secret", _contract()))


def test_volume_lookup_failure_does_not_become_an_allocation_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api

    monkeypatch.setattr(api, "get_network_volumes", lambda _api_key: [])
    with pytest.raises(TargetObservationError, match="cannot be uniquely confirmed"):
        asyncio.run(resolve_existing_volume("secret", "astrid-release"))


def test_release_deletes_exact_pod_only_after_quiescence_and_proves_volume_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api

    handle_path = tmp_path / "claim-handle.json"
    handle = _claim_handle()
    pods = [_pod()]
    deleted: list[str] = []
    gate_calls: list[str] = []

    def list_pods(_api_key: str) -> list[dict[str, Any]]:
        return list(pods)

    def terminate_pod(pod_id: str, _api_key: str) -> None:
        deleted.append(pod_id)
        pods.clear()

    async def quiesce_worker(record: dict[str, Any]) -> None:
        disk = json.loads(handle_path.read_text(encoding="utf-8"))
        assert disk["state"] == "release_pending"
        assert record["pod_id"] == "pod-exact"
        gate_calls.append(record["operation_id"])
        return _quiescence(record)

    monkeypatch.setattr(api, "list_pods", list_pods)
    monkeypatch.setattr(api, "get_network_volumes", lambda _key: [_volume()])
    monkeypatch.setattr(api, "terminate_pod", terminate_pod)

    write_target_record(handle_path, handle)
    result = asyncio.run(
        _release_exact_target_after_quiescence(
            SimpleNamespace(api_key="test-key"),
            handle_path=handle_path,
            attempt=handle,
            quiesce_worker=quiesce_worker,
            verify_timeout_seconds=0,
        )
    )

    assert result is not None
    assert gate_calls == ["operation-1"]
    assert deleted == ["pod-exact"]
    assert result["state"] == "cleanup_complete"
    assert result["cleanup"] == {
        "pod_id": "pod-exact",
        "status": "terminated",
        "backup_volume_id": "vol-persist",
        "backup_volume_preserved": True,
        "confirmed_at": result["cleanup"]["confirmed_at"],
    }
    assert json.loads(handle_path.read_text(encoding="utf-8"))["cleanup_status"] == "complete"


def test_release_accepts_exact_owned_pod_when_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api

    handle_path = tmp_path / "claim-handle.json"
    handle = _claim_handle()
    pods = [_pod(desiredStatus="STOPPED", actualStatus="STOPPED")]
    deleted: list[str] = []

    def terminate_pod(pod_id: str, _api_key: str) -> None:
        deleted.append(pod_id)
        pods.clear()

    monkeypatch.setattr(api, "list_pods", lambda _key: list(pods))
    monkeypatch.setattr(api, "get_network_volumes", lambda _key: [_volume()])
    monkeypatch.setattr(api, "terminate_pod", terminate_pod)

    async def quiesce_worker(record: dict[str, Any]) -> None:
        return _quiescence(record)

    result = _run_release(
        handle_path, handle, quiesce_worker=quiesce_worker
    )

    assert deleted == ["pod-exact"]
    assert result["state"] == "cleanup_complete"


def test_release_completes_already_absent_exact_pod_with_persisted_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api

    handle_path = tmp_path / "claim-handle.json"
    deleted: list[str] = []
    monkeypatch.setattr(api, "list_pods", lambda _key: [])
    monkeypatch.setattr(api, "get_network_volumes", lambda _key: [_volume()])
    monkeypatch.setattr(api, "terminate_pod", lambda pod_id, _key: deleted.append(pod_id))

    async def quiesce_worker(record: dict[str, Any]) -> None:
        return _quiescence(record)

    result = _run_release(
        handle_path, _claim_handle(), quiesce_worker=quiesce_worker
    )

    assert deleted == []
    assert result["cleanup"]["status"] == "already_gone"
    assert result["cleanup"]["backup_volume_preserved"] is True


def test_release_requires_explicit_attached_pod_authority_before_any_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api

    handle_path = tmp_path / "claim-handle.json"
    handle = _claim_handle(acquisition_mode="attach", release_authorized=False)
    write_target_record(handle_path, handle)
    monkeypatch.setattr(api, "list_pods", lambda _key: pytest.fail("provider must not be called"))

    async def quiesce_worker(_record: dict[str, Any]) -> None:
        pytest.fail("worker must not be touched")

    with pytest.raises(TargetObservationError, match="exclusive release authority"):
        asyncio.run(
            _release_exact_target_after_quiescence(
                SimpleNamespace(api_key="test-key"),
                handle_path=handle_path,
                attempt=handle,
                quiesce_worker=quiesce_worker,
            )
        )

    assert json.loads(handle_path.read_text(encoding="utf-8"))["state"] == "claimed"


def test_release_gate_failure_leaves_exact_pod_and_resumeable_release_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api

    handle_path = tmp_path / "claim-handle.json"
    handle = _claim_handle()
    monkeypatch.setattr(api, "list_pods", lambda _key: pytest.fail("delete must not be reached"))

    async def blocked(_record: dict[str, Any]) -> None:
        raise RuntimeError("active or uncertain attempt")

    write_target_record(handle_path, handle)
    with pytest.raises(RuntimeError, match="active or uncertain"):
        asyncio.run(
            _release_exact_target_after_quiescence(
                SimpleNamespace(api_key="test-key"),
                handle_path=handle_path,
                attempt=handle,
                quiesce_worker=blocked,
            )
        )

    current = json.loads(handle_path.read_text(encoding="utf-8"))
    assert current["state"] == "release_pending"
    assert current["last_release_gate_failure_type"] == "RuntimeError"


def test_release_does_not_delete_if_volume_identity_cannot_be_reconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api

    handle_path = tmp_path / "claim-handle.json"
    handle = _claim_handle()
    deletes: list[str] = []
    monkeypatch.setattr(api, "list_pods", lambda _key: [_pod()])
    # An empty result is ambiguous because Lifecycle can degrade lookup errors
    # to an empty collection. It is not proof that the volume is absent/safe.
    monkeypatch.setattr(api, "get_network_volumes", lambda _key: [])
    monkeypatch.setattr(api, "terminate_pod", lambda pod_id, _key: deletes.append(pod_id))

    async def quiesce_worker(record: dict[str, Any]) -> None:
        return _quiescence(record)

    write_target_record(handle_path, handle)
    with pytest.raises(TargetObservationError, match="cannot be confirmed"):
        asyncio.run(
            _release_exact_target_after_quiescence(
                SimpleNamespace(api_key="test-key"),
                handle_path=handle_path,
                attempt=handle,
                quiesce_worker=quiesce_worker,
            )
        )

    assert deletes == []
    assert json.loads(handle_path.read_text(encoding="utf-8"))["state"] == "release_pending"


def test_release_does_not_claim_volume_preservation_after_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api

    handle_path = tmp_path / "claim-handle.json"
    handle = _claim_handle()
    pods = [_pod()]
    volume_reads = 0
    deleted: list[str] = []

    def volumes(_key: str) -> list[dict[str, Any]]:
        nonlocal volume_reads
        volume_reads += 1
        if volume_reads == 1:
            return [_volume()]
        return [_volume(size=500)]

    def terminate(pod_id: str, _key: str) -> None:
        deleted.append(pod_id)
        pods.clear()

    monkeypatch.setattr(api, "list_pods", lambda _key: list(pods))
    monkeypatch.setattr(api, "get_network_volumes", volumes)
    monkeypatch.setattr(api, "terminate_pod", terminate)

    async def quiesce_worker(record: dict[str, Any]) -> None:
        return _quiescence(record)

    write_target_record(handle_path, handle)
    with pytest.raises(TargetObservationError, match="size mismatch"):
        asyncio.run(
            _release_exact_target_after_quiescence(
                SimpleNamespace(api_key="test-key"),
                handle_path=handle_path,
                attempt=handle,
                quiesce_worker=quiesce_worker,
                verify_timeout_seconds=0,
            )
        )

    assert deleted == ["pod-exact"]
    current = json.loads(handle_path.read_text(encoding="utf-8"))
    assert current["state"] == "cleanup_pending"
    assert "cleanup" not in current


def test_resumed_release_retries_only_the_same_present_pod_after_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api
    from runpod_lifecycle.errors import CleanupPendingError

    handle_path = tmp_path / "claim-handle.json"
    handle = _claim_handle(state="cleanup_pending")
    pods = [_pod()]
    reconciled: list[str] = []
    deleted: list[str] = []

    def reconcile(pod_id: str, _key: str, **_kwargs: Any) -> None:
        reconciled.append(pod_id)
        raise CleanupPendingError(pod_id, "still present")

    def terminate(pod_id: str, _key: str) -> None:
        deleted.append(pod_id)
        pods.clear()

    monkeypatch.setattr(api, "reconcile_pod_cleanup", reconcile)
    monkeypatch.setattr(api, "list_pods", lambda _key: list(pods))
    monkeypatch.setattr(api, "get_network_volumes", lambda _key: [_volume()])
    monkeypatch.setattr(api, "terminate_pod", terminate)

    async def quiesce_worker(record: dict[str, Any]) -> None:
        assert record["pod_id"] == "pod-exact"
        return _quiescence(record)

    result = _run_release(
        handle_path,
        handle,
        quiesce_worker=quiesce_worker,
        resume=True,
    )

    assert reconciled == ["pod-exact"]
    assert deleted == ["pod-exact"]
    assert result["state"] == "cleanup_complete"


def test_resumed_release_reconciles_lost_delete_reply_before_any_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import api
    from runpod_lifecycle.errors import CleanupPendingError

    handle_path = tmp_path / "claim-handle.json"
    handle = _claim_handle()
    pods = [_pod()]
    deletes: list[str] = []
    quiesce_calls: list[str] = []

    def terminate(pod_id: str, _key: str) -> None:
        deletes.append(pod_id)
        pods.clear()
        raise CleanupPendingError(pod_id, "delete response was lost")

    monkeypatch.setattr(api, "list_pods", lambda _key: list(pods))
    monkeypatch.setattr(api, "get_network_volumes", lambda _key: [_volume()])
    monkeypatch.setattr(api, "terminate_pod", terminate)

    async def quiesce_worker(record: dict[str, Any]) -> None:
        quiesce_calls.append(record["operation_id"])
        return _quiescence(record)

    write_target_record(handle_path, handle)
    with pytest.raises(CleanupPendingError):
        asyncio.run(
            _release_exact_target_after_quiescence(
                SimpleNamespace(api_key="test-key"),
                handle_path=handle_path,
                attempt=handle,
                quiesce_worker=quiesce_worker,
                verify_timeout_seconds=0,
            )
        )
    interrupted = json.loads(handle_path.read_text(encoding="utf-8"))
    assert interrupted["state"] == "cleanup_pending"

    # On explicit resume, exact-ID absence is observed first and no second
    # delete is issued; the persistent volume is independently reconfirmed.
    resumed = _run_release(
        handle_path,
        interrupted,
        quiesce_worker=quiesce_worker,
        resume=True,
    )
    assert deletes == ["pod-exact"]
    assert quiesce_calls == ["operation-1"]
    assert resumed["state"] == "cleanup_complete"
    assert resumed["cleanup"]["backup_volume_preserved"] is True


def test_acquisition_owner_rejects_pending_journal_before_any_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runpod_lifecycle

    handle_path = tmp_path / "attempt.json"
    attempt = {
        "schema_version": "astrid.runpod.allocation-attempt.v1",
        "operation_id": "existing-operation",
        "state": "allocation_unknown",
        "request_name": "astrid-prepare-existing-operation",
    }
    handle_path.write_text(json.dumps(attempt), encoding="utf-8")
    launches: list[str] = []

    async def forbidden_launch(_config: Any, *, name: str, **_kwargs: Any) -> Any:
        launches.append(name)
        raise AssertionError("an unresolved operation cannot create a second target")

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", forbidden_launch)
    config = SimpleNamespace(
        api_key="test-key",
        gpu_type_candidates=("NVIDIA GeForce RTX 5090",),
        worker_image="runpod/base:cuda1300",
    )

    with pytest.raises(AcquisitionUnresolvedError, match="may already be in flight"):
        asyncio.run(
            acquire_target(
                config,
                handle_path=handle_path,
                attempt=attempt,
                volume={"id": "vol-persist", "name": "astrid-release", "size": 250},
                pod_id=None,
                max_wait_seconds=1,
                capacity_window_seconds=1,
                retry_interval_seconds=1,
                ready_timeout_seconds=1,
            )
        )

    assert launches == []
