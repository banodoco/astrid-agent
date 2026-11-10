"""A display label is never substituted for the accepted provider catalog ID."""
import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import target_selection, worker_preparation as preparation
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
    attempt = {"operation_id": "operation-exact", "request_name": "astrid-product-operation-exact",
               "state": "allocated_unverified" if resume else "allocation_ready",
               "acquisition_mode": "allocate", "gpu_type": "A40", "accepted_gpu_type_id": "NVIDIA A40",
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
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", launch)
    monkeypatch.setattr(runpod_lifecycle, "get_pod", get)
    monkeypatch.setattr(api, "list_pods", lambda key: [inventory])
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [])
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
        assert caught.value.gate == "GPU"
        failed = json.loads(path.read_text())
        assert failed["failure_gate"] == "GPU" and failed["reconciliation_required"] is True
        assert failed.get("target_identity_verified") is not True
    assert calls.count("create") == (0 if resume else 1)
    assert calls.count("get") == (1 if resume else 0)
    assert calls.count("catalog") == 1
    saved = json.loads(path.read_text())
    assert saved["gpu_type"] == "A40" and saved["accepted_gpu_type_id"] == "NVIDIA A40"


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

    async def fail(*args, **kwargs):
        raise preparation.TargetObservationError("secret-provider-message", gate=gate)

    monkeypatch.setattr(target_selection, "acquire_target", SimpleNamespace(__wrapped__=fail))
    # The actual acquisition failure checkpoint and the outer selection outcome
    # apply the same fixed-name boundary, including to a malformed gate.
    error = preparation.TargetObservationError("secret-provider-message", gate=gate)
    owner = c.fresh()
    with pytest.raises(preparation.AcquisitionUnresolvedError) as caught:
        owner.acquire(c.grant)
    assert caught.value.gate == expected and f"failed gate: {expected}" in str(caught.value)
    record = c.read()
    preparation._write_target_failure(c.owner.path, record, operation_id=record["operation_id"],
                                      phase="provider_contract", exc=error)
    assert c.read()["failure_gate"] == expected
    assert "secret-provider" not in str(caught.value) and "secret-provider" not in c.owner.path.read_text()
