"""Offline regression cases for v2 transitional and stopped pod states."""

import asyncio
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from runpod_lifecycle import api

from astrid.core.execution.provider_route_resolvers import (
    ProviderRouteResolutionError,
    resolve_runpod_pod_handle_ssh_route,
)
from astrid.packs.runpod import pod_watchdog, worker_owner
from astrid.packs.runpod import worker_preparation as preparation
from astrid.packs.runpod.provider import parse_rest_pod


def pod(status="STARTING", **updates):
    return {
        "id": "review-pod", "name": "review-owned", "status": status,
        "image": "worker:image", "gpu": {"id": "NVIDIA GeForce RTX 5090", "count": 1},
        "dataCenterId": "EU-RO-1", "cost": 1.0, "runtime": None,
        "mounts": {}, "actions": ["stop", "terminate"], **updates,
    }


def contract():
    return preparation.RunPodTargetContract(
        pod_id="review-pod", expected_pod_name="review-owned",
        gpu_type="NVIDIA GeForce RTX 5090", worker_image="worker:image",
        network_volume_id=None, network_volume_name=None, network_volume_size_gb=0,
        datacenter_id="EU-RO-1",
    )


def test_terminate_action_is_not_write_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_POD_ID", "fake-pod")
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-key")
    log = tmp_path / "engine.log"
    monkeypatch.setattr(pod_watchdog, "IDENTITY_LOG_PATH", log)
    methods = []

    def read(request, **kwargs):
        class Response(io.BytesIO):
            status = 200

        methods.append(request.get_method())
        assert request.get_method() == "GET"
        return Response(b'{"status":"RUNNING","actions":["stop","terminate"]}')

    monkeypatch.setattr(pod_watchdog, "urlopen", read)
    assert pod_watchdog._report_identity() == "unknown"
    assert methods == ["GET"]
    assert "termination_authorised=unknown" in log.read_text()
    assert "watchdog=unavailable custody=host-only" in log.read_text()
    assert "probe_status=200" in log.read_text()


@pytest.mark.parametrize("compatibility", [False, True])
def test_starting_pod_remains_observable_during_acquisition(monkeypatch, compatibility):
    raw = pod(**({"actualStatus": "STARTING"} if compatibility else {}))
    monkeypatch.setattr(api, "_get_runpod", lambda: None)
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert url == api.PODS_URL
        return httpx.Response(200, json={
            "pods": [raw], "pagination": {"hasNextPage": False},
        }, request=httpx.Request("GET", url))

    monkeypatch.setattr(api.httpx, "get", get)
    direct = preparation.inspect_target_records(contract(), pods=[raw], volumes=[])
    observed = preparation.observe_target("fake", contract())
    assert direct.pod_id == observed.pod_id == "review-pod"
    assert observed.worker_image == "worker:image"
    assert calls == [api.PODS_URL]


def test_starting_without_live_ssh_cannot_be_used_as_a_route(tmp_path, monkeypatch):
    handle = tmp_path / "handle.json"
    handle.write_text(json.dumps({"pod_id": "review-pod", "ssh": "root@192.0.2.1 -p 2222"}))
    monkeypatch.setattr(
        "astrid.core.util.credentials_scope.CredentialsScope.resolve_local",
        staticmethod(lambda *args, **kwargs: SimpleNamespace(value="fake")),
    )
    monkeypatch.setattr(api, "get_pod_status", lambda *_args: {
        "desired_status": "STARTING", "ports": [],
    })
    with pytest.raises(ProviderRouteResolutionError, match="no live SSH endpoint"):
        resolve_runpod_pod_handle_ssh_route(handle)


def test_exited_pod_is_stopped():
    raw = pod("EXITED", actions=["start", "terminate"])
    assert parse_rest_pod(raw).state == "stopped"


def test_exited_pod_remains_owned(monkeypatch):
    raw = pod("EXITED", actions=["start", "terminate"])
    monkeypatch.setattr(preparation, "read_exact_pod", lambda *_args: (200, api.normalize_pod(raw)))
    assert worker_owner.provider_pod_state("fake", "review-pod") == "stopped"


def test_exited_exact_get_cannot_prove_release_absence(monkeypatch):
    raw = pod("EXITED", actions=["start", "terminate"])
    monkeypatch.setattr(preparation, "read_exact_pod", lambda *_args: (200, raw))
    # Even a successful empty inventory cannot override the exact stopped pod.
    monkeypatch.setattr(api, "list_pods", lambda _key: [])
    with pytest.raises(preparation.TargetObservationError, match="GET404"):
        asyncio.run(preparation._confirm_release_absence(
            SimpleNamespace(api_key="fake"), contract(),
            {"provider_account_ref": "review-account", "acquisition_mode": "allocate"},
        ))
