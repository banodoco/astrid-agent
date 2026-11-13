"""Acquisition is confirmed from GraphQL pod.machine, never from REST.

Live pod 65dvlpzs5ejrnk: REST GET /v2/pods/{id} reports ``machine: {}`` (no
gpuTypeId, no dataCenterId), so confirming from REST always failed a correct
pod. The datacenter must equal the contract's exactly; the GPU passes only if
the provider's own gpuTypes catalog displayName for the contract's GPU type id
equals ``machine.gpuDisplayName`` exactly. A missing GraphQL machine at the
readiness timeout fails closed and the existing release path releases the pod.
No sockets, keys or pods: the two GraphQL calls are the exact recorded payloads.
"""
import asyncio
import socket

import pytest

from astrid.packs.runpod import worker_preparation as preparation
from astrid.packs.runpod.worker_owner import RunPodWorkerOwnerError
from astrid.packs.runpod.worker_preparation import (
    AcquisitionUnresolvedError,
    RunPodTargetContract,
    TargetObservationError,
)
from tests.packs.runpod.test_warm_session import small_lease as small_lease, lease_selection_case as lease_selection_case  # noqa: F401

POD_ID = "65dvlpzs5ejrnk"
GPU_ID = "NVIDIA GeForce RTX 5090"
IMAGE = "runpod/base:cuda1300"

POD_GRAPHQL = {"data": {"pod": {"machine": {"gpuDisplayName": "RTX 5090", "dataCenterId": "EU-RO-1"}}}}
GPU_TYPES = {"data": {"gpuTypes": [{"id": GPU_ID, "displayName": "RTX 5090"}]}}

pytestmark = pytest.mark.usefixtures("real_acquisition_confirmation")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network forbidden"))
    monkeypatch.setattr(preparation, "_MACHINE_POLL_SECONDS", 0.01, raising=False)


def _contract(**overrides):
    return RunPodTargetContract(
        pod_id=POD_ID, gpu_type=GPU_ID, worker_image=IMAGE, network_volume_id=None,
        network_volume_name=None, network_volume_size_gb=0, expected_pod_name="astrid-product-op",
        datacenter_id="EU-RO-1", **overrides)


def _rest_pod():
    # REST GET /v2/pods/{id} (and the inventory record) for the live pod: machine == {}.
    return {"id": POD_ID, "name": "astrid-product-op", "desiredStatus": "RUNNING",
            "actualStatus": "RUNNING", "imageName": IMAGE, "machine": {}}


@pytest.fixture
def provider(monkeypatch):
    """REST says machine {}; GraphQL answers with the recorded payloads."""
    import runpod.api.ctl_commands as ctl
    import runpod.api.graphql as sdk_graphql
    from runpod_lifecycle import api

    state = {"queries": [], "rest_reads": 0, "pod": POD_GRAPHQL, "gpus": GPU_TYPES}

    def run_graphql_query(query, api_key=None):
        state["queries"].append(query)
        if "gpuTypes" in query:
            assert f'id: "{GPU_ID}"' in query
            return state["gpus"]
        assert f'podId: "{POD_ID}"' in query
        return state["pod"]

    def rest_read(key, pod_id):
        state["rest_reads"] += 1
        return 200, _rest_pod()

    monkeypatch.setattr(sdk_graphql, "run_graphql_query", run_graphql_query)
    monkeypatch.setattr(ctl, "run_graphql_query", run_graphql_query)
    monkeypatch.setattr(api, "list_pods", lambda key: [_rest_pod()])
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [])
    monkeypatch.setattr(preparation, "read_exact_pod", rest_read)
    return state


def _acquire(contract=None, timeout=0):
    return asyncio.run(preparation.observe_and_confirm_acquired_target(
        "fake", contract or _contract(), ready_timeout_seconds=timeout))


def test_acquisition_passes_from_graphql_machine_even_though_rest_reports_machine_empty(provider):
    observation = _acquire()
    assert observation.pod_id == POD_ID
    assert observation.gpu_type == "RTX 5090" and observation.datacenter_id == "EU-RO-1"
    assert provider["rest_reads"] == 0  # REST is not consulted for the decision
    assert [("gpuTypes" in q) for q in provider["queries"]] == [False, True]  # pod, then the catalog


def test_datacenter_mismatch_refuses(provider):
    provider["pod"] = {"data": {"pod": {"machine": {"gpuDisplayName": "RTX 5090", "dataCenterId": "US-XX-1"}}}}
    with pytest.raises(TargetObservationError, match="datacenter mismatch"):
        _acquire()


def test_gpu_mismatch_refuses(provider):
    provider["pod"] = {"data": {"pod": {"machine": {"gpuDisplayName": "RTX 4090", "dataCenterId": "EU-RO-1"}}}}
    with pytest.raises(TargetObservationError, match="GPU mismatch"):
        _acquire()


@pytest.mark.parametrize("reported", ["rtx 5090", "RTX  5090", "NVIDIA GeForce RTX 5090"])
def test_gpu_match_is_exact_not_fuzzy(provider, reported):
    provider["pod"] = {"data": {"pod": {"machine": {"gpuDisplayName": reported, "dataCenterId": "EU-RO-1"}}}}
    with pytest.raises(TargetObservationError, match="GPU mismatch"):
        _acquire()


def test_unknown_gpu_type_in_provider_catalog_refuses(provider):
    provider["gpus"] = {"data": {"gpuTypes": []}}
    with pytest.raises(TargetObservationError, match="gpuTypes catalog"):
        _acquire()


def test_missing_graphql_machine_after_timeout_fails_closed(provider):
    provider["pod"] = {"data": {"pod": {"machine": None}}}
    with pytest.raises(TargetObservationError, match="machine was not reported"):
        _acquire(timeout=0.05)
    assert len(provider["queries"]) >= 2  # it polled until the readiness timeout


def test_missing_graphql_machine_fails_acquisition_and_the_release_path_is_reached(small_lease, monkeypatch):
    import runpod
    from runpod_lifecycle import api, graphql
    c = small_lease
    monkeypatch.setattr(api, "list_pods", lambda key: [{**pod, "machine": {}} for pod in c.pods])  # REST: machine {}
    monkeypatch.setattr(graphql, "get_pod", lambda pod_id, key=None: {"machine": None})
    monkeypatch.setattr(runpod, "get_gpu", lambda gpu_id, *a, **k: {"id": gpu_id, "displayName": "RTX 5090"})
    real = preparation.confirm_acquired_machine  # the lease's readiness timeout is minutes; keep the test short
    monkeypatch.setattr(preparation, "confirm_acquired_machine",
                        lambda key, contract, *, timeout_seconds: real(key, contract, timeout_seconds=0.05))
    owner = c.fresh()

    # 80bbf690: the exact created pod is released synchronously on this failure.
    with pytest.raises(RunPodWorkerOwnerError,
                       match="RunPod acquisition failed; created pod pod-1 was released") as caught:
        owner.acquire(c.grant)

    failed = c.read()
    assert isinstance(caught.value.__cause__, AcquisitionUnresolvedError)
    assert failed["pod_id"] == "pod-1" and failed["failure_phase"] == "provider_contract"
    assert failed["failure_type"] == "TargetObservationError"
    record = c.read()
    assert c.deletes == ["pod-1"], record.get("cleanup_failure_type")
    assert record["state"] == "cleanup_complete"
    assert record["cleanup"]["pod_id"] == "pod-1"
    assert record["lease"]["release_reason"] == "custody_changed"
    assert c.creates == 1 and c.pods == []
