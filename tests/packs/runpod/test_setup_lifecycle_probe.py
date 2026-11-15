"""Setup uses the actual lifecycle public probe with provider IO substituted."""

import asyncio
import importlib

import pytest

from astrid.packs.runpod import adapter


@pytest.fixture(autouse=True)
def no_provider_io(monkeypatch):
    import runpod_lifecycle

    async def unavailable(*args, **kwargs):
        return []

    monkeypatch.setattr(runpod_lifecycle, "probe", unavailable)


def test_setup_inventory_uses_datacenter_offerings(monkeypatch):
    lifecycle_availability = importlib.import_module("runpod_lifecycle.availability")
    gpus = [
        {"id": "gpu-1", "displayName": "GPU One", "memoryInGb": 24,
         "lowestPrice": {"uninterruptablePrice": 0.5}},
        {"id": "gpu-2", "displayName": "GPU Two", "memoryInGb": 24,
         "lowestPrice": {"uninterruptablePrice": 0.8}},
    ]

    async def centers(api_key, constraints):
        return [{"id": "DC-A", "gpuAvailability": [
                    {"gpuTypeId": "gpu-1", "available": False, "stockStatus": "none"}]},
                {"id": "DC-B", "gpuAvailability": [
                    {"gpuTypeId": "gpu-2", "available": True, "stockStatus": "available"}]}]

    monkeypatch.setattr(lifecycle_availability, "_fetch_datacenters", centers)
    from runpod.api import graphql as sdk_graphql

    monkeypatch.setattr(sdk_graphql, "run_graphql_query", lambda query, api_key: {
        "data": {"gpuTypes": gpus},
    })

    assert asyncio.run(adapter.setup_datacenters("fixture-key")) == ["DC-A", "DC-B"]
    assert asyncio.run(adapter.setup_gpu_types(
        "fixture-key", datacenter_id=None, require_network_volume=False,
    )) == ["GPU One", "GPU Two"]
    dc_a = asyncio.run(adapter.setup_gpu_types(
        "fixture-key", datacenter_id="DC-A", require_network_volume=False,
    ))
    assert dc_a == ["GPU One"]
    assert dc_a.stock_missing == ("GPU One",)


def test_setup_without_datacenter_uses_union_of_offerings(monkeypatch):
    from runpod.api import graphql as sdk_graphql
    availability = importlib.import_module("runpod_lifecycle.availability")
    monkeypatch.setattr(sdk_graphql, "run_graphql_query", lambda query, api_key: {
        "data": {"gpuTypes": [{"id": "a", "displayName": "GPU A"},
                               {"id": "b", "displayName": "GPU B"},
                               {"id": "c", "displayName": "GPU C"}]},
    })
    async def centers(api_key, constraints):
        return [{"id": "DC-A", "gpuAvailability": [{"gpuTypeId": "a", "stockStatus": "low"}]},
                {"id": "DC-B", "gpuAvailability": [{"gpuTypeId": "b", "stockStatus": "none"}]}]
    monkeypatch.setattr(availability, "_fetch_datacenters", centers)
    assert asyncio.run(adapter.setup_gpu_types(
        "fixture-key", datacenter_id=None, require_network_volume=False,
    )) == ["GPU A", "GPU B"]


def test_setup_gpu_catalog_survives_empty_stock_and_records_guidance(monkeypatch):
    from runpod.api import graphql as sdk_graphql
    availability = importlib.import_module("runpod_lifecycle.availability")

    monkeypatch.setattr(sdk_graphql, "run_graphql_query", lambda query, api_key: {
        "data": {"gpuTypes": [{"id": "gpu-5090", "displayName": "RTX 5090"}]},
    })

    async def no_stock(api_key, constraints):
        return [{"id": "EUR-NO-1", "gpuAvailability": [
            {"gpuTypeId": "gpu-5090", "available": False, "stockStatus": "none"}]}]
    monkeypatch.setattr(availability, "_fetch_datacenters", no_stock)
    choices = asyncio.run(adapter.setup_gpu_types(
        "fixture-key", datacenter_id="EUR-NO-1", require_network_volume=False,
    ))
    assert choices == ["RTX 5090"]
    assert choices.stock_missing == ("RTX 5090",)


def test_empty_datacenter_rows_fall_back_to_catalog_with_warning(monkeypatch):
    from runpod.api import graphql as sdk_graphql
    availability = importlib.import_module("runpod_lifecycle.availability")
    monkeypatch.setattr(sdk_graphql, "run_graphql_query", lambda query, api_key: {
        "data": {"gpuTypes": [{"id": "a", "displayName": "GPU A"}]},
    })
    async def empty(api_key, constraints):
        return [{"id": "DC-A", "gpuAvailability": []}]
    monkeypatch.setattr(availability, "_fetch_datacenters", empty)
    choices = asyncio.run(adapter.setup_gpu_types("fixture-key", datacenter_id="DC-A",
                                                  require_network_volume=True))
    assert choices == ["GPU A"]
    assert choices.availability_fallback is True


def test_datacenter_query_failure_is_sanitized(monkeypatch):
    import pytest
    from runpod.api import graphql as sdk_graphql
    availability = importlib.import_module("runpod_lifecycle.availability")
    monkeypatch.setattr(sdk_graphql, "run_graphql_query", lambda query, api_key: {
        "data": {"gpuTypes": [{"id": "a", "displayName": "GPU A"}]},
    })
    async def failed(api_key, constraints):
        raise RuntimeError("secret provider response")
    monkeypatch.setattr(availability, "_fetch_datacenters", failed)
    with pytest.raises(RuntimeError, match="RunPod datacenter catalog is unavailable") as exc:
        asyncio.run(adapter.setup_gpu_types("fixture-key", datacenter_id="DC-A",
                                            require_network_volume=True))
    assert "secret" not in str(exc.value)


def test_setup_gpu_catalog_rejects_unknown_gpu_and_sanitizes_catalog_failure(monkeypatch):
    import pytest
    from runpod.api import graphql as sdk_graphql
    availability = importlib.import_module("runpod_lifecycle.availability")

    monkeypatch.setattr(sdk_graphql, "run_graphql_query", lambda query, api_key: {
        "data": {"gpuTypes": [{"id": "gpu-1", "displayName": "GPU One"}]},
    })
    async def centers(api_key, constraints):
        return [{"id": "DC-A", "gpuAvailability": [{"gpuTypeId": "gpu-1", "available": True}]}]
    monkeypatch.setattr(availability, "_fetch_datacenters", centers)
    choices = asyncio.run(adapter.setup_gpu_types(
        "fixture-key", datacenter_id=None, require_network_volume=True,
    ))
    assert "GPU Missing" not in choices

    def failed_query(query, api_key):
        raise RuntimeError("provider body and credential must not escape")

    monkeypatch.setattr(sdk_graphql, "run_graphql_query", failed_query)
    with pytest.raises(RuntimeError, match="RunPod GPU catalog is unavailable; preferences were not saved"):
        asyncio.run(adapter.setup_gpu_types(
            "fixture-key", datacenter_id=None, require_network_volume=False,
        ))
