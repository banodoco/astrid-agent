"""A display label is never substituted for the accepted provider catalog ID."""
import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import worker_preparation as preparation
from astrid.packs.runpod.worker_owner import RunPodWorkerOwnerError
from tests.packs.runpod.test_warm_session import small_lease as small_lease, lease_selection_case as lease_selection_case  # noqa: F401

pytestmark = pytest.mark.usefixtures("real_acquisition_confirmation")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network forbidden"))


@pytest.mark.parametrize("resume", [False, True], ids=["acquire", "reconcile"])
@pytest.mark.parametrize("reported", ["A40", "A100"], ids=["accepted", "mismatch"])
def test_catalog_id_survives_display_label_in_acquisition_and_reconciliation(tmp_path, monkeypatch, resume, reported):
    import runpod_lifecycle
    from runpod_lifecycle import api, graphql
    import runpod.api.graphql as sdk_graphql

    config = SimpleNamespace(api_key="fake", gpu_type_candidates=("A40",), gpu_type_id="NVIDIA A40",
                             worker_image="image-exact")
    volume = {"id": None, "name": None, "size": 0, "dataCenterId": "CA-MTL-1"}
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    attempt = {"paid_limits": {"hourly_usd": 1.0, "max_usd": 1.0,
                               "deadline": expires_at, "expires_at": expires_at},
               "operation_id": "operation-exact", "request_name": "astrid-product-operation-exact",
               "state": "allocated_unverified" if resume else "allocation_ready",
               "acquisition_mode": "allocate", "gpu_type": "A40", "accepted_gpu_type_id": "NVIDIA A40",
               "schema_version": "astrid.runpod.allocation-attempt.v1", "provider_account_ref": "account-exact",
               "release_authorized": True, "worker_activation": "not_started", "worker_image": config.worker_image,
               "storage_mode": "ephemeral", "network_volume_id": None, "network_volume_name": None,
               "network_volume_size_gb": 0,
               "create_attempts": 1 if resume else 0, **({"pod_id": "pod-exact"} if resume else {})}
    path = tmp_path / "claim.json"
    preparation.write_target_record(path, attempt)
    calls = []

    async def ready(**kwargs):
        calls.append("ready")

    pod = SimpleNamespace(id="pod-exact", name=attempt["request_name"], wait_ready=ready)

    async def launch(*args, **kwargs):
        calls.append("create")
        return pod

    async def get(*args, **kwargs):
        calls.append("get")
        return pod

    inventory = {"id": pod.id, "name": pod.name, "desiredStatus": "RUNNING",
                 "imageName": config.worker_image, "machine": {}}
    pods, deletes = [inventory], []

    def terminate(pod_id, key):
        deletes.append(pod_id)
        pods[:] = [row for row in pods if row["id"] != pod_id]

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", launch)
    monkeypatch.setattr(runpod_lifecycle, "get_pod", get)
    monkeypatch.setattr(preparation, "_get_provider_pod", get)
    monkeypatch.setattr(api, "list_pods", lambda key: list(pods))
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [])
    monkeypatch.setattr(api, "terminate_pod", terminate)
    monkeypatch.setattr(preparation, "read_exact_pod", lambda key, pod_id: (
        (200, dict(inventory)) if any(row["id"] == pod_id for row in pods) else (404, None)))
    monkeypatch.setattr(graphql, "get_pod", lambda *args: {
        "machine": {"gpuDisplayName": reported, "dataCenterId": "CA-MTL-1"}})

    def catalog(query, **kwargs):
        assert 'id: "NVIDIA A40"' in query
        calls.append("catalog")
        return {"data": {"gpuTypes": [{"id": "NVIDIA A40", "displayName": "A40"}]}}

    monkeypatch.setattr(sdk_graphql, "run_graphql_query", catalog)
    operation = preparation.reconcile_target if resume else preparation.acquire_target
    run = operation(config, handle_path=path, attempt=attempt, volume=volume, ready_timeout_seconds=1,
                    **({} if resume else {"pod_id": None, "max_wait_seconds": 0,
                                         "capacity_window_seconds": 60, "retry_interval_seconds": 15}))
    if reported == "A40":
        acquired = asyncio.run(run)
        assert acquired.observation.gpu_type == "A40"
        assert acquired.attempt["target_identity_verified"] is True
    else:
        with pytest.raises(preparation.TargetObservationError, match="GPU mismatch") as caught:
            asyncio.run(run)
        # 0a7de39c: shared gates are provider-neutral; retain the exact RunPod gate.
        assert caught.value.gate == "hardware"
        assert caught.value.runpod_gate == "GPU"
        assert preparation.observation_failure_gate(caught.value) == "GPU"
        failed = json.loads(path.read_text())
        assert failed["failure_gate"] == "GPU" and failed["reconciliation_required"] is True
        assert failed.get("target_identity_verified") is not True
    assert calls.count("create") == (0 if resume else 1)
    assert calls.count("get") == (1 if resume else 0)
    assert calls.count("catalog") == 1
    saved = json.loads(path.read_text())
    assert saved["gpu_type"] == "A40" and saved["accepted_gpu_type_id"] == "NVIDIA A40"

    async def quiesce(current):
        return preparation.unactivated_target_quiescence(current)

    released = asyncio.run(preparation._release_exact_target_after_quiescence(
        config, handle_path=path, attempt=saved, quiesce_worker=quiesce))
    assert deletes == ["pod-exact"]
    assert released["state"] == "cleanup_complete"
    assert json.loads(path.read_text())["state"] == "cleanup_complete"
    assert released["cleanup"]["pod_id"] == "pod-exact"


@pytest.mark.parametrize("canonical", ["absent", None, "NVIDIA A40"])
def test_legacy_config_without_canonical_id_keeps_existing_candidate(canonical):
    config = SimpleNamespace(gpu_type_candidates=("A40",), worker_image="image")
    if canonical != "absent":
        config.gpu_type_id = canonical
    contract = preparation._target_contract(config, pod_id="pod", expected_name="name",
                                             volume={"id": None, "name": None, "size": 0})
    assert contract.gpu_type == ("NVIDIA A40" if canonical == "NVIDIA A40" else "A40")


@pytest.mark.parametrize("gate,expected", [("GPU", "GPU"), ("secret-provider-value", "provider"),
                                           ({"secret": "value"}, "provider"), (None, "provider")])
def test_only_fixed_gate_names_reach_journal_and_outcome(small_lease, monkeypatch, gate, expected):
    c = small_lease

    def fail(*args, **kwargs):
        raise error

    # 0a7de39c: hardware/inventory are shared gates, GPU/provider are RunPod gates.
    # Fail after the fake create so the real failure checkpoint also proves cleanup.
    monkeypatch.setattr(preparation, "confirm_acquired_machine", fail)
    # The actual acquisition failure checkpoint and the outer selection outcome
    # apply the same fixed-name boundary, including to a malformed gate.
    error = preparation.TargetObservationError("secret-provider-message", gate=gate)
    if not isinstance(gate, str):
        assert error.gate == "inventory"
    owner = c.fresh()
    error_type = RunPodWorkerOwnerError if expected == "GPU" else preparation.AcquisitionUnresolvedError
    with pytest.raises(error_type) as caught:
        owner.acquire(c.grant)
    # 80bbf690: a GPU failure is wrapped only after synchronous exact release.
    failure = caught.value.__cause__ if expected == "GPU" else caught.value
    assert isinstance(failure, preparation.AcquisitionUnresolvedError)
    assert failure.gate == ("hardware" if expected == "GPU" else "inventory")
    assert failure.runpod_gate == expected
    assert preparation.observation_failure_gate(failure) == expected
    assert preparation.observation_failure_gate(error) == expected
    assert f"failed gate: {expected}" in str(failure)
    assert c.read()["failure_gate"] == expected
    assert "secret-provider" not in str(caught.value) and "secret-provider" not in c.owner.path.read_text()
    c.clock.advance(10)  # f0535fef: provider-gate recovery waits at least five seconds.
    assert c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["cleanup"]["pod_id"] == "pod-1"
    assert c.creates == 1
