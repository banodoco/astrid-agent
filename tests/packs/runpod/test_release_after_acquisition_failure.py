"""A pod this product created is released on the acquisition-failure path.

Regression for live attempt 2 (pod 65dvlpzs5ejrnk, task ba4f0b97...): create
returned a pod id, the journal recorded it (allocated_unverified), then the
post-ready provider-contract check raised TargetObservationError because the
account inventory (lifecycle ``list_pods`` prefers the GraphQL projection)
reported ``machine.gpuDisplayName`` "RTX 5090" while the journal holds the GPU
type id "NVIDIA GeForce RTX 5090". Selection surfaced
"provider acquisition remains unresolved; reconcile the same journal". The
lease owner's recovery release then ran the same GPU comparison in release
mode; 74bc2500d only relaxed it when the provider reports *no* GPU, so a
reported-but-differently-spelled GPU kept the journal in cleanup_pending with
cleanup_failure_type TargetObservationError and the paid pod RUNNING.

Release is keyed on the create response's pod id (plus the journal's unique
request name, image and volume); GPU and datacenter labels are evidence, not
release gates. No sockets, keys or pods.
"""
import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import worker_preparation as preparation
from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError
from tests.packs.runpod.test_warm_session import small_lease as small_lease, lease_selection_case as lease_selection_case  # noqa: F401


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network forbidden"))


def _graphql_projection(pod):
    # lifecycle.graphql._POD_FIELDS: no machineType; machine{gpuDisplayName, dataCenterId}.
    projected = {key: value for key, value in pod.items() if key != "machineType"}
    projected["machine"] = {"gpuDisplayName": "Preferred GPU", "dataCenterId": "EU-RO-1"}
    return projected


def test_readiness_timeout_is_visible_and_releases_only_the_created_pod(small_lease, monkeypatch):
    import runpod_lifecycle

    c = small_lease
    launch = runpod_lifecycle.launch_when_available
    timeouts = []
    secret = "provider-token-and-private-response"

    async def unready_launch(*args, **kwargs):
        pod = await launch(*args, **kwargs)

        async def unready(*, timeout):
            timeouts.append(timeout)
            raise runpod_lifecycle.NotReadyTimeout(secret)

        pod.wait_ready = unready
        return pod

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", unready_launch)
    owner = c.fresh()
    with pytest.raises(AcquisitionUnresolvedError, match="did not become ready within the startup timeout") as caught:
        owner.acquire(c.grant)

    assert secret not in str(caught.value)
    assert "same journal" in str(caught.value)
    assert timeouts == [900]
    failed = c.read()
    assert failed["pod_id"] == "pod-1" and failed["failure_phase"] == "readiness"
    assert failed["failure_type"] == "NotReadyTimeout"
    assert secret not in json.dumps(failed)
    assert c.creates == 1 and not c.deletes

    c.clock.advance(1)

    record = c.read()
    assert c.deletes == ["pod-1"]
    assert record["state"] == "cleanup_complete"
    assert record["operation_id"] == failed["operation_id"]
    assert record["cleanup"]["pod_id"] == "pod-1"
    assert record["lease"]["release_reason"] == "recovery"
    assert c.creates == 1


def test_acquisition_failure_releases_the_created_pod_by_create_response_id(
        small_lease, monkeypatch, real_acquisition_confirmation):
    import runpod
    from runpod_lifecycle import api, graphql
    c = small_lease
    monkeypatch.setattr(api, "list_pods", lambda key: [_graphql_projection(pod) for pod in c.pods])
    # Acquisition confirms from GraphQL pod.machine + the gpuTypes catalog: the
    # machine reports "Preferred GPU" but the catalog names the type "RTX 5090".
    monkeypatch.setattr(graphql, "get_pod", lambda pod_id, key=None: {
        "machine": {"gpuDisplayName": "Preferred GPU", "dataCenterId": "EU-RO-1"}})
    import runpod.api.graphql as sdk_graphql
    monkeypatch.setattr(sdk_graphql, "run_graphql_query", lambda query, api_key=None: {"data": {"gpuTypes": [
        {"id": "NVIDIA GeForce RTX 5090", "displayName": "RTX 5090"}]}})
    owner = c.fresh()

    with pytest.raises(AcquisitionUnresolvedError, match="provider acquisition remains unresolved"):
        owner.acquire(c.grant)

    failed = c.read()
    assert c.creates == 1 and [pod["id"] for pod in c.pods] == ["pod-1"]
    # The create response's id is journaled before any acquisition check.
    assert failed["pod_id"] == "pod-1" and failed["failure_phase"] == "provider_contract"
    assert failed["failure_type"] == "TargetObservationError"
    assert not c.deletes

    c.clock.advance(1)  # the lease owner's scheduled recovery release

    record = c.read()
    assert c.deletes == ["pod-1"], record.get("cleanup_failure_type")
    assert record["state"] == "cleanup_complete"
    assert record["cleanup"]["pod_id"] == "pod-1"
    assert record["lease"]["release_reason"] == "recovery"
    assert c.creates == 1  # release never creates a replacement


def _attempt2_journal():
    """The live journal shape: ephemeral, EU-RO-1, drained lease, stuck cleanup_pending."""
    lease = {"schema": "astrid.runpod.lease.v1", "state": "releasing", "release_reason": "recovery",
             "generation": 1, "owner_epoch": "e" * 32, "invocation_id": "task:attempt:1",
             "idle_seconds": 300.0, "started_at": 1794084032.0}
    record = {
        "schema_version": "astrid.runpod.allocation-attempt.v1", "state": "cleanup_pending",
        "operation_id": "b3bb", "request_name": "astrid-product-b3bb", "name": "astrid-product-b3bb",
        "pod_id": "65dvlpzs5ejrnk", "provider_account_ref": "runpod-default",
        "acquisition_mode": "allocate", "release_authorized": True, "worker_activation": "not_started",
        "gpu_type": "NVIDIA GeForce RTX 5090", "accepted_gpu_type_id": "NVIDIA GeForce RTX 5090",
        "worker_image": "runpod/base:cuda1300@sha256:" + "8" * 64,
        "storage_mode": "ephemeral", "network_volume_id": None, "network_volume_name": None,
        "network_volume_size_gb": 0, "network_volume_datacenter_id": "EU-RO-1",
        "failure_phase": "provider_contract", "failure_type": "TargetObservationError",
        "cleanup_failure_type": "TargetObservationError", "reconciliation_required": True,
        "lease": lease,
    }
    record["release_quiescence"] = {
        "schema": "astrid.runpod.lease-drain.v1", "state": "drained", "admission_closed": True,
        **{key: record[key] for key in ("operation_id", "pod_id", "provider_account_ref")},
        **{key: lease[key] for key in ("owner_epoch", "generation", "invocation_id")},
    }
    return record


def _provider(monkeypatch, pod):
    from runpod_lifecycle import api
    state = {"alive": True, "deleted": []}

    def terminate(pod_id, key):
        state["alive"] = False
        state["deleted"].append(pod_id)

    monkeypatch.setattr(api, "list_pods", lambda key: [dict(pod)] if state["alive"] else [])
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [])
    monkeypatch.setattr(api, "terminate_pod", terminate)
    monkeypatch.setattr(api, "reconcile_pod_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(preparation, "read_exact_pod",
                        lambda key, pod_id: (200, dict(pod)) if state["alive"] and pod["id"] == pod_id
                        else (404, None))
    return state


def _resume(path, record):
    async def forbidden(current):
        pytest.fail("saved exact drain must not replay")
    return asyncio.run(preparation._release_exact_target_after_quiescence(
        SimpleNamespace(api_key="fake"), handle_path=path, attempt=record,
        quiesce_worker=forbidden, resume=True))


def _live_pod(**overrides):
    record = _attempt2_journal()
    return {"id": record["pod_id"], "name": record["request_name"], "desiredStatus": "RUNNING",
            "imageName": record["worker_image"],
            "machine": {"gpuDisplayName": "RTX 5090", "dataCenterId": "EU-RO-1"}, **overrides}


@pytest.mark.parametrize("machine", [
    {"gpuDisplayName": "RTX 5090", "dataCenterId": "EU-RO-1"},   # GraphQL display name
    {"gpuDisplayName": "RTX 5090", "dataCenterId": "US-TX-3"},   # datacenter label differs
    {},                                                          # REST: unreported
])
def test_stuck_attempt2_journal_resumes_to_termination(tmp_path, monkeypatch, machine):
    path = tmp_path / "claim.json"
    record = _attempt2_journal()
    preparation.write_target_record(path, record)
    state = _provider(monkeypatch, _live_pod(machine=machine))
    result = _resume(path, record)
    assert state["deleted"] == ["65dvlpzs5ejrnk"]
    assert result["state"] == "cleanup_complete"
    assert json.loads(path.read_text())["cleanup"]["pod_id"] == "65dvlpzs5ejrnk"


@pytest.mark.parametrize("change", [{"id": "someone-elses-pod"}, {"name": "astrid-product-other"},
                                    {"imageName": "other/image:1"}, {"networkVolumeId": "vol-x"}])
def test_release_still_refuses_a_pod_that_is_not_the_created_one(tmp_path, monkeypatch, change):
    path = tmp_path / "claim.json"
    record = _attempt2_journal()
    preparation.write_target_record(path, record)
    state = _provider(monkeypatch, _live_pod(**change))
    if "id" in change:
        # The created id is absent from inventory: absence is proved, nothing is deleted.
        result = _resume(path, record)
        assert result["state"] == "cleanup_complete" and result["cleanup"]["status"] == "already_gone"
    else:
        with pytest.raises(preparation.TargetObservationError):
            _resume(path, record)
    assert state["deleted"] == []
