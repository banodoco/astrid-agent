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
from pathlib import Path

import pytest

from astrid.packs.runpod import target_selection as selection
from astrid.packs.runpod import worker_preparation as preparation
from astrid.packs.runpod.target_selection import PaidRunConsent, PaidSelectionError, RunPodOffer, RunPodSelectionOwner
from astrid.packs.runpod.worker_preparation import RunPodTargetObservation
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401

POD_ID = "vbkci4pniqyb4v"
GPU_ID = "NVIDIA GeForce RTX 5090"
# Recorded: attempt-3 GraphQL pod.machine, gpuTypes catalog row, and the REST GET shape.
POD_GRAPHQL = {"data": {"pod": {"gpuCount": 1, "machine": {"gpuDisplayName": "RTX 5090", "dataCenterId": "EU-RO-1"}}}}
GPU_TYPES = {"data": {"gpuTypes": [{"id": GPU_ID, "displayName": "RTX 5090"}]}}

pytestmark = pytest.mark.usefixtures("real_acquisition_confirmation")


class FakeTime:
    """Stands in for the time module inside worker_preparation: sleep advances monotonic."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def __getattr__(self, name):
        import time
        return getattr(time, name)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network forbidden"))
    monkeypatch.setattr(preparation, "_MACHINE_POLL_SECONDS", 3.0, raising=False)
    # Fake clock: the bounded GraphQL wait polls on this time, so the tests stay fast.
    monkeypatch.setattr(preparation, "time", FakeTime(), raising=False)


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
    fixture = Path(__file__).parents[2] / "providers/runpod/fixtures/rest-get-pod-include-network-volume.json"
    rest = json.loads(fixture.read_text())
    rest.update(id=POD_ID, networkVolumeId=case.prefs.volume_id)
    rest["networkVolume"]["id"] = case.prefs.volume_id
    state = SimpleNamespace(case=case, pod=POD_GRAPHQL, gpus=GPU_TYPES, rest=rest)

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


# ---- gate naming, bounded GraphQL wait, pod-total price (010f0dae1 -> this change) ----
import datetime as _dt


def _set_graphql_pod(gate, monkeypatch, responder):
    """Route pod queries through ``responder(n)`` (n = 1-based pod query count); count them."""
    import runpod.api.ctl_commands as ctl
    import runpod.api.graphql as sdk_graphql
    seen = []

    def run_graphql_query(query, api_key=None):
        if "gpuTypes" in query:
            return gate.gpus
        seen.append(query)
        return responder(len(seen))

    monkeypatch.setattr(sdk_graphql, "run_graphql_query", run_graphql_query)
    monkeypatch.setattr(ctl, "run_graphql_query", run_graphql_query)
    return seen


def _past_expiry(gate, rate):
    def observed(pod):
        gate.case.now = _dt.datetime(2026, 11, 1, 5, 0, tzinfo=_dt.timezone.utc)
        return rate
    return observed


@pytest.mark.parametrize("name", ["volume", "GPU", "datacenter", "rate", "lifetime", "machine"])
def test_each_failing_gate_names_itself_in_the_error_and_the_journal(gate, name):
    if name == "volume":
        gate.rest["volumeMountPath"] = "/wrong"
    elif name == "GPU":
        gate.pod = {"data": {"pod": {"gpuCount": 1, "machine": {"gpuDisplayName": "RTX 4090", "dataCenterId": "EU-RO-1"}}}}
    elif name == "datacenter":
        gate.pod = {"data": {"pod": {"gpuCount": 1, "machine": {"gpuDisplayName": "RTX 5090", "dataCenterId": "US-XX-1"}}}}
    elif name == "machine":
        gate.pod = {"data": {"pod": {"gpuCount": 1, "machine": None}}}
    elif name == "rate":
        gate.case.owner.observed_hourly_usd = lambda pod: 9.99
    elif name == "lifetime":
        gate.case.owner.observed_hourly_usd = _past_expiry(gate, 1.19)
    with pytest.raises(PaidSelectionError) as caught:
        _select(gate)
    assert f"unconfirmed {name} gate" in str(caught.value)
    assert "price or lifetime" not in str(caught.value)
    assert caught.value.runpod_gate == name
    assert caught.value.gate == {"volume": "storage", "GPU": "hardware", "datacenter": "region",
                                 "machine": "hardware"}.get(name, name)
    assert caught.value.custody == "exact"
    record = _journal(gate)
    assert record["state"] == "cleanup_pending" and record["failed_gate"] == name
    assert record["pod_id"] == POD_ID  # release behavior unchanged


@pytest.mark.parametrize("appears_on", [2, 3])
def test_machine_that_appears_after_one_or_two_polls_passes(gate, monkeypatch, appears_on):
    ready = {"data": {"pod": {"gpuCount": 1, "machine": {"gpuDisplayName": "RTX 5090", "dataCenterId": "EU-RO-1"}}}}
    empty = {"data": {"pod": {"gpuCount": 1, "machine": None}}}
    seen = _set_graphql_pod(gate, monkeypatch, lambda n: empty if n < appears_on else ready)
    assert _select(gate)["state"] == "claimed"
    assert len(seen) >= appears_on


def test_machine_that_never_appears_fails_closed_after_the_timeout_not_the_first_poll(gate, monkeypatch):
    empty = {"data": {"pod": {"gpuCount": 1, "machine": None}}}
    seen = _set_graphql_pod(gate, monkeypatch, lambda n: empty)
    start = preparation.time.now
    with pytest.raises(PaidSelectionError, match="unconfirmed machine gate"):
        _select(gate)
    assert len(seen) > 2
    assert preparation.time.now - start >= selection._READINESS_TIMEOUT_SECONDS - preparation._MACHINE_POLL_SECONDS
    assert set(preparation.time.sleeps) == {preparation._MACHINE_POLL_SECONDS}
    assert _journal(gate)["state"] == "cleanup_pending"


@pytest.mark.parametrize("observed,count,offer,ok", [
    (1.19, 1, 1.19, True),       # recorded: gpuCount 1, costPerHr 1.19, offer 1.19
    (2.38, 2, 1.19, True),       # pod total of two GPUs
    (2.38, 1, 1.19, False),
    (1.19, 1, "1.190", True),    # same price, different spelling
    ("1.190", 1, 1.19, True),
    (1.2, 1, 1.19, False),
    (1.18, 1, 1.19, True),       # cheaper than the offer never blocks
    (0.1 + 0.2, 1, 0.3, False),  # a float-noise value is a different, higher price; no tolerance
    (3.57, 3, 1.19, True),       # 1.19 * 3 is 3.5700000000000003 in float math
])
def test_pod_total_rate_is_compared_in_decimal(observed, count, offer, ok):
    assert selection.pod_rate_within_offer(observed, offer, count) is ok


@pytest.mark.parametrize("count", [None, 0, -1, 1.0, True, "1"])
def test_unknown_gpu_count_fails_closed_in_the_rate_gate(count):
    with pytest.raises(PaidSelectionError) as caught:
        selection.pod_rate_within_offer(1.19, 1.19, count)
    assert caught.value.gate == "rate"


@pytest.mark.parametrize("observed,count,passes", [(1.19, 1, True), (2.38, 2, True), (2.38, 1, False)])
def test_selection_prices_the_pod_total_from_graphql_gpu_count(gate, observed, count, passes):
    gate.pod = {"data": {"pod": {"gpuCount": count, "machine": {"gpuDisplayName": "RTX 5090", "dataCenterId": "EU-RO-1"}}}}
    gate.case.owner.observed_hourly_usd = lambda pod: observed
    if passes:
        assert _select(gate)["state"] == "claimed"
    else:
        with pytest.raises(PaidSelectionError, match="unconfirmed rate gate"):
            _select(gate)
        assert _journal(gate)["failed_gate"] == "rate"


def test_selection_fails_closed_when_graphql_reports_no_gpu_count(gate):
    gate.pod = {"data": {"pod": {"machine": {"gpuDisplayName": "RTX 5090", "dataCenterId": "EU-RO-1"}}}}
    with pytest.raises(PaidSelectionError, match="unconfirmed rate gate"):
        _select(gate)
    assert _journal(gate)["state"] == "cleanup_pending"
