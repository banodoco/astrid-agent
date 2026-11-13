"""Offline wire contracts from RunPod's v2 migration guide and OpenAPI schema."""

import httpx
import pytest
from runpod_lifecycle import api
from runpod_lifecycle.discovery import normalize_pod_mounts

from astrid.packs.runpod import h3_release as build
from astrid.packs.runpod.pod_fields import InvalidPodField, pod_field
from astrid.packs.runpod.provider import RunPodMachineProvider, parse_rest_pod
from astrid.packs.runpod.provider_urls import NETWORK_VOLUMES_URL, PODS_URL, REST_BASE_URL
from astrid.packs.runpod.worker_preparation import RunPodTargetContract, inspect_target_records

GPU = "NVIDIA GeForce RTX 5090"


def pod(**updates):
    return {"id": "pod", "name": "owned", "status": "RUNNING", "image": "worker:image",
            "cost": 1.19, "disk": 20, "gpu": {"id": GPU, "count": 2},
            "dataCenterId": "EU-RO-1", "actions": ["stop", "terminate"],
            "mounts": {"network": [{"volumeId": "vol", "path": "/workspace"}]},
            "runtime": {"uptime": 10, "ports": [
                {"private": 22, "public": 12345, "type": "tcp", "ip": "192.0.2.1"}]},
            **updates}


def page(items, cursor=None):
    return {"pods": items, "pagination": {"nextCursor": cursor, "hasNextPage": cursor is not None}}


def test_shared_endpoints_and_native_pod_evidence():
    assert PODS_URL == api.PODS_URL == "https://api.runpod.io/v2/pods"
    assert NETWORK_VOLUMES_URL == api.NETWORK_VOLUMES_URL == "https://api.runpod.io/v2/network-volumes"
    raw = pod()
    record = parse_rest_pod(raw)
    assert record.machine_id == "pod" and record.state == "running"
    assert record.require("image") == "worker:image"
    assert record.require("storage_id") == "vol" and record.require("storage_mount") == "/workspace"
    assert record.require("gpu_count") == 2 and record.require("gpu_label") == GPU
    assert record.require("hourly_usd") == 1.19 and record.require("region") == "EU-RO-1"
    assert pod_field(raw, "volume") == "vol"
    assert pod_field(raw, "gpu_count") == 2 and pod_field(raw, "gpu_name") == GPU
    with pytest.raises(InvalidPodField):
        pod_field(pod(networkVolumeId="another"), "volume")
    cpu = pod(gpu=None, cpu={"id": "cpu5c", "vcpuCount": 2})
    del cpu["gpu"]
    assert parse_rest_pod(cpu).require("gpu_count") == 0


def test_exact_read_and_complete_inventory_are_v2(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/pod"):
            assert "params" not in kwargs
            body = pod()
        elif kwargs["params"].get("cursor") == "opaque/+?":
            body = page([pod(runtime=None)])
        else:
            body = page([pod(id="other", name="unrelated", runtime=None)], "opaque/+?")
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    provider = RunPodMachineProvider("fake")
    assert provider.read("pod").require("storage_id") == "vol"
    records = provider.find("owned")
    assert [r.machine_id for r in records] == ["pod"]
    assert calls[-1][1]["params"] == {"cursor": "opaque/+?"}
    assert all(url.startswith(PODS_URL) for url, _ in calls)


def test_incomplete_inventory_raises_instead_of_proving_absence(monkeypatch):
    def get(url, **kwargs):
        status = 503 if kwargs["params"] else 200
        return httpx.Response(status, json=page([], "next"), request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(httpx.HTTPStatusError):
        RunPodMachineProvider("fake").find("owned")


def test_worker_observation_and_h3_attachment_use_v2_volume_fields():
    volume = {"id": "vol", "name": "storage", "size": 250, "dataCenter": "EU-RO-1", "type": "STANDARD"}
    contract = RunPodTargetContract(pod_id="pod", expected_pod_name="owned", gpu_type=GPU,
        worker_image="worker:image", network_volume_id="vol", network_volume_name="storage",
        network_volume_size_gb=250, datacenter_id="EU-RO-1")
    observed = inspect_target_records(contract, pods=[pod()], volumes=[volume])
    assert observed.pod_id == "pod" and observed.datacenter_id == "EU-RO-1"
    normalized = build.normalize_volume(volume, "vol")
    assert normalized["datacenter"] == "EU-RO-1"
    build.check_attachment(pod(), normalized, "worker:image", GPU)
    with pytest.raises(build.BuildError):
        build.check_attachment(pod(dataCenterId="US-TX-3"), normalized, "worker:image", GPU)
    with pytest.raises(ValueError):
        # Configured references cannot replace the independent typed observation.
        normalize_pod_mounts(pod(), "pod", "a" * 32)


def test_h3_native_get_volume_paginated_find_and_delete():
    calls = []
    def handle(request):
        calls.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("network-volumes/vol"):
            return httpx.Response(200, json={"id": "vol", "size": 250, "dataCenter": "EU-RO-1"})
        if request.url.path.endswith("pods/pod"):
            return httpx.Response(200, json=pod())
        return httpx.Response(200, json=(page([pod(runtime=None)]) if request.url.params.get("cursor")
                                       else page([], "next")))
    provider = build.RunPodProvider("fake")
    provider.http.close()
    provider.http = httpx.Client(base_url=REST_BASE_URL, transport=httpx.MockTransport(handle))
    try:
        assert provider.volume("vol")["dataCenterId"] == "EU-RO-1"
        assert provider.get("pod")["costPerHr"] == 1.19
        assert [p["id"] for p in provider.find("owned")] == ["pod"]
        provider.delete("pod")
    finally:
        provider.http.close()
    assert [(r.method, str(r.url)) for r in calls][-1] == ("DELETE", PODS_URL + "/pod")
    assert all(r.url.host == "api.runpod.io" and r.url.path.startswith("/v2/") for r in calls)


@pytest.mark.parametrize("direct", [False, True])
def test_h3_ssh_route_uses_v2_runtime_or_direct_endpoint(monkeypatch, direct):
    from runpod_lifecycle import ssh
    connected = []
    class Client:
        def __init__(self, *args, **kwargs):
            connected.append((args, kwargs))
        def connect(self):
            pass
        def disconnect(self):
            pass
    monkeypatch.setattr(ssh, "SSHClient", Client)
    monkeypatch.setenv("RUNPOD_SSH_KEY_PATH", "/test/key")
    raw = pod(runtime=None, ssh={"direct": {"host": "192.0.2.1", "port": 12345,
                                             "username": "root", "command": "ssh"}}) if direct else pod()
    transport = build.LifecycleTransport(raw, 2)
    transport.close()
    assert connected[0][0] == ("192.0.2.1", 12345, "root")
    assert connected[0][1]["private_key_path"] == "/test/key"
