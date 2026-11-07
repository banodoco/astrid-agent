"""A created pod whose provider record omits machine/GPU/datacenter must still be released.

Regression for the stuck live release: the journal held a created pod
(state release_pending/cleanup_pending, ephemeral storage) while RunPod reported
``machine: {}`` and no GPU or datacenter. Release re-ran the full attach
contract, raised TargetObservationError on every retry and never terminated
the pod. No sockets, keys or pods.
"""
import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import worker_preparation as preparation
from tests.packs.runpod.test_worker_preparation import _claim_handle, _quiescence


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network forbidden"))


def _created_record(**overrides):
    record = _claim_handle(
        state="cleanup_pending", name="astrid-product-op", request_name="astrid-product-op",
        pod_id="pod-created", storage_mode="ephemeral", network_volume_id=None,
        network_volume_name=None, network_volume_size_gb=0, network_volume_datacenter_id="EU-RO-1",
        reconciliation_required=True, **overrides)
    record["release_quiescence"] = _quiescence(record)
    return record


def _unreported_pod(**overrides):
    # Shape observed live: no machineType, machine == {}, no dataCenterId.
    return {"id": "pod-created", "name": "astrid-product-op", "desiredStatus": "RUNNING",
            "imageName": "runpod/base:cuda1300", "machine": {}, **overrides}


def _fake_provider(monkeypatch, pod):
    from runpod_lifecycle import api
    state = {"alive": True, "deleted": []}

    def list_pods(key):
        return [dict(pod)] if state["alive"] else []

    def terminate(pod_id, key):
        assert pod_id == "pod-created"
        state["alive"] = False
        state["deleted"].append(pod_id)

    monkeypatch.setattr(api, "list_pods", list_pods)
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [])
    monkeypatch.setattr(api, "terminate_pod", terminate)
    monkeypatch.setattr(api, "reconcile_pod_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(preparation, "read_exact_pod",
                        lambda key, pod_id: (404, None) if not state["alive"] else (200, dict(pod)))
    return state


async def _forbidden(current):
    pytest.fail("saved exact quiescence must not replay")


def _release(path, record):
    return asyncio.run(preparation._release_exact_target_after_quiescence(
        SimpleNamespace(api_key="fake"), handle_path=path, attempt=record,
        quiesce_worker=_forbidden, resume=True))


def test_created_pod_with_unreported_machine_is_terminated_on_resume(tmp_path, monkeypatch):
    path = tmp_path / "claim.json"
    record = _created_record()
    preparation.write_target_record(path, record)
    state = _fake_provider(monkeypatch, _unreported_pod())
    result = _release(path, record)
    assert state["deleted"] == ["pod-created"]
    assert result["state"] == "cleanup_complete"
    assert json.loads(path.read_text())["state"] == "cleanup_complete"


# GPU/datacenter labels are evidence, not release gates (see
# test_release_after_acquisition_failure.py); identity is id+name+image+volume.
@pytest.mark.parametrize("change", [{"name": "someone-elses-pod"}, {"imageName": "other/image:1"},
                                    {"networkVolumeId": "vol-other"}])
def test_release_still_refuses_a_pod_that_contradicts_the_journal(tmp_path, monkeypatch, change):
    path = tmp_path / "claim.json"
    record = _created_record()
    preparation.write_target_record(path, record)
    state = _fake_provider(monkeypatch, _unreported_pod(**change))
    with pytest.raises(preparation.TargetObservationError):
        _release(path, record)
    assert state["deleted"] == []
