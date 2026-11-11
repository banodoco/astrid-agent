"""Setup uses the actual lifecycle public probe with provider IO substituted."""

import asyncio
import importlib

from astrid.packs.runpod import adapter


def test_setup_inventory_uses_public_lifecycle_probe(monkeypatch):
    import runpod_lifecycle
    from runpod_lifecycle.availability import DataCenterAvailability

    lifecycle_probe = importlib.import_module("runpod_lifecycle.probe")
    assert runpod_lifecycle.probe is lifecycle_probe.probe

    calls = []
    gpus = [
        {"id": "gpu-1", "displayName": "GPU One", "memoryInGb": 24,
         "lowestPrice": {"uninterruptablePrice": 0.5}},
        {"id": "gpu-2", "displayName": "GPU Two", "memoryInGb": 24,
         "lowestPrice": {"uninterruptablePrice": 0.8}},
    ]

    async def fetch(api_key, require_secure_cloud, allowed_cuda_versions=None):
        calls.append(("catalogue", api_key))
        return gpus

    async def query(api_key, query_text, variables):
        calls.append(("filtered_catalogue", api_key))
        return {"gpuTypes": gpus}

    async def centers(api_key, constraints):
        calls.append(("centers", api_key))
        return []

    async def per_gpu(api_key, gpu_id, centers, constraints, datacenter_ids, include_price):
        dc = "DC-A" if gpu_id == "gpu-1" else "DC-B"
        return [DataCenterAvailability(dc, None, None, "available", None, 0.5, True)]

    monkeypatch.setattr(lifecycle_probe, "_fetch_gpu_types", fetch)
    monkeypatch.setattr(lifecycle_probe, "_query", query)
    monkeypatch.setattr(lifecycle_probe, "_fetch_datacenters", centers)
    monkeypatch.setattr(lifecycle_probe, "_for_gpu", per_gpu)

    assert asyncio.run(adapter.setup_datacenters("fixture-key")) == ["DC-A", "DC-B"]
    assert asyncio.run(adapter.setup_gpu_types(
        "fixture-key", datacenter_id=None, require_network_volume=False,
    )) == ["GPU One", "GPU Two"]
    assert asyncio.run(adapter.setup_gpu_types(
        "fixture-key", datacenter_id="DC-A", require_network_volume=False,
    )) == ["GPU One"]
    assert ("catalogue", "fixture-key") in calls
    assert ("filtered_catalogue", "fixture-key") in calls
