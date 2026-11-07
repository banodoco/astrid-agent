"""Every post-acquisition gate in RunPodSelectionOwner reads fields RunPod actually returns.

Attempt 3 (pod vbkci4pniqyb4v) passed the GraphQL GPU/datacenter confirmation in
worker_preparation and then stopped at "unconfirmed price or lifetime": the selection
gate re-checked GPU id and datacenter on the REST pod, which has no gpuTypeId and no
dataCenterId (``machine: {}`` on a correct fresh pod). The gate now confirms from
GraphQL pod.machine and the gpuTypes catalog (recorded payloads below), and a failed
gate fails closed to cleanup_pending (release) naming the gate. No sockets, keys, pods.
"""
import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import target_selection as selection
from astrid.packs.runpod import worker_preparation as preparation
from astrid.packs.runpod.target_selection import PaidRunConsent, PaidSelectionError, RunPodOffer, RunPodSelectionOwner
from astrid.packs.runpod.worker_preparation import RunPodTargetObservation
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401

POD_ID = "vbkci4pniqyb4v"
GPU_ID = "NVIDIA GeForce RTX 5090"
# Recorded: attempt-3 GraphQL pod.machine, gpuTypes catalog row, and the REST GET shape.
POD_GRAPHQL = {"data": {"pod": {"machine": {"gpuDisplayName": "RTX 5090", "dataCenterId": "EU-RO-1"}}}}
GPU_TYPES = {"data": {"gpuTypes": [{"id": GPU_ID, "displayName": "RTX 5090"}]}}

pytestmark = pytest.mark.usefixtures("real_acquisition_confirmation")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network forbidden"))
    monkeypatch.setattr(preparation, "_MACHINE_POLL_SECONDS", 0.01, raising=False)


@pytest.fixture
def gate(case, monkeypatch):  # noqa: F811
    import runpod.api.ctl_commands as ctl
    import runpod.api.graphql as sdk_graphql
    from runpod_lifecycle import api

    offer = RunPodOffer("account-a", "preferred-gpu", 1.19, "offer-1", "EU-RO-1", GPU_ID)
    case.offer, case.rate = offer, 1.19
    case.owner.observe_offer = lambda: offer
    case.owner.observed_hourly_usd = lambda pod: 1.19
    case.grant = PaidRunConsent(
        "invocation-1", offer.digest(case.prefs), 3.0, "2026-11-01T02:00:00Z", ("paid",), 30, 30, 60)
    state = SimpleNamespace(case=case, pod=POD_GRAPHQL, gpus=GPU_TYPES,
                            rest={"id": POD_ID, "volumeMountPath": "/workspace", "networkVolumeId": case.prefs.volume_id,
                                  "machine": {}})

    def run_graphql_query(query, api_key=None):
        return state.gpus if "gpuTypes" in query else state.pod

    async def acquire(config, *, attempt, **kwargs):
        observation = RunPodTargetObservation(
            POD_ID, attempt["request_name"], "RUNNING", None, "RTX 5090", case.prefs.image,
            case.prefs.volume_id, case.prefs.volume_name, case.prefs.volume_size_gb, "EU-RO-1", "2026-11-01T00:00:00Z")
        return SimpleNamespace(attempt={**attempt, "pod_id": POD_ID, "state": "allocated_unverified"},
                               observation=observation)

    monkeypatch.setattr(sdk_graphql, "run_graphql_query", run_graphql_query)
    monkeypatch.setattr(ctl, "run_graphql_query", run_graphql_query)
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [
        {"id": case.prefs.volume_id, "name": case.prefs.volume_name, "size": case.prefs.volume_size_gb,
         "dataCenterId": "EU-RO-1"}])
    monkeypatch.setattr(selection, "acquire_target", SimpleNamespace(__wrapped__=acquire))
    monkeypatch.setattr(selection, "read_exact_pod", lambda key, pod: (200, dict(state.rest)))
    return state


def _select(gate):
    return asyncio.run(gate.case.owner.select(gate.case.grant))


def _journal(gate):
    return json.loads(gate.case.owner.path.read_text())


def test_gpu_and_datacenter_pass_from_graphql_when_rest_has_no_gpu_or_datacenter_fields(gate):
    claimed = _select(gate)
    assert claimed["state"] == "claimed" and claimed["pod_id"] == POD_ID


def test_rest_gpu_and_datacenter_are_not_trusted_over_graphql(gate):
    gate.rest.update(gpuTypeId=GPU_ID, dataCenterId="EU-RO-1", machine={"gpuTypeId": GPU_ID, "dataCenterId": "EU-RO-1"})
    gate.pod = {"data": {"pod": {"machine": {"gpuDisplayName": "RTX 4090", "dataCenterId": "EU-RO-1"}}}}
    with pytest.raises(PaidSelectionError, match="GPU mismatch"):
        _select(gate)
    assert _journal(gate)["state"] == "cleanup_pending"


def test_datacenter_mismatch_fails_closed_with_release_and_names_the_gate(gate):
    gate.pod = {"data": {"pod": {"machine": {"gpuDisplayName": "RTX 5090", "dataCenterId": "US-XX-1"}}}}
    with pytest.raises(PaidSelectionError, match="datacenter mismatch"):
        _select(gate)
    record = _journal(gate)
    assert record["state"] == "cleanup_pending" and record["reconciliation_required"] is True
    assert record["pod_id"] == POD_ID


def test_unreported_graphql_machine_fails_closed_with_release(gate):
    gate.pod = {"data": {"pod": {"machine": None}}}
    with pytest.raises(PaidSelectionError, match="machine was not reported"):
        _select(gate)
    assert _journal(gate)["state"] == "cleanup_pending"


def test_gpu_not_in_provider_catalog_fails_closed_with_release(gate):
    gate.gpus = {"data": {"gpuTypes": []}}
    with pytest.raises(PaidSelectionError, match="gpuTypes catalog"):
        _select(gate)
    assert _journal(gate)["state"] == "cleanup_pending"
