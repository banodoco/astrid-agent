"""Independent product absence evidence; no sockets, keys, provider calls or pods."""
import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from astrid.packs.runpod import worker_preparation as preparation
from astrid.packs.runpod.remote_placement import LifecyclePodProvider, RemotePlacementError
from tests.packs.runpod.test_worker_preparation import _claim_handle, _pod, _volume, _quiescence


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network forbidden"))


@pytest.mark.parametrize("get_result", [(404, None), (200, _pod()), (200, None), (404, {}),
                                        (401, None), (500, None), TimeoutError("secret-provider-message")])
@pytest.mark.parametrize("listed", [[], [_pod()], None, [{}], [_pod(), _pod()],
                                   TimeoutError("secret-list-message")])
def test_delete404_still_requires_independent_get404_and_successful_list_absence(
    tmp_path, monkeypatch, get_result, listed,
):
    from runpod_lifecycle import api
    path = tmp_path / "claim.json"
    record = _claim_handle()
    preparation.write_target_record(path, record)
    calls = []

    def inventory(key):
        calls.append("list")
        if calls.count("list") == 1:
            return [_pod()]
        if isinstance(listed, Exception):
            raise listed
        return listed

    def exact(key, pod):
        assert pod == record["pod_id"]
        calls.append("get")
        if isinstance(get_result, Exception):
            raise get_result
        return get_result

    def delete(pod, key):
        assert pod == record["pod_id"]
        calls.append("delete404")  # Lifecycle's DELETE404 branch returns normally.

    async def quiesce(current):
        return _quiescence(current)

    monkeypatch.setattr(api, "list_pods", inventory)
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [_volume()])
    monkeypatch.setattr(api, "terminate_pod", delete)
    monkeypatch.setattr(preparation, "read_exact_pod", exact)
    success = get_result == (404, None) and listed == []
    if success:
        result = asyncio.run(preparation._release_exact_target_after_quiescence(
            SimpleNamespace(api_key="fake"), handle_path=path, attempt=record, quiesce_worker=quiesce,
        ))
        assert result["state"] == "cleanup_complete"
        assert result["cleanup"]["absence"] == {
            "pod_id": record["pod_id"], "provider_account_ref": record["provider_account_ref"],
            "exact_get_status": 404, "account_list_succeeded": True, "account_list_absent": True,
        }
    else:
        with pytest.raises((preparation.TargetObservationError, TimeoutError)):
            asyncio.run(preparation._release_exact_target_after_quiescence(
                SimpleNamespace(api_key="fake"), handle_path=path, attempt=record, quiesce_worker=quiesce,
            ))
        result = json.loads(path.read_text())
        assert result["state"] == "cleanup_pending"
        assert result["reconciliation_required"] is True
        assert "cleanup" not in result
        assert "secret-" not in path.read_text()
    assert calls[:3] == ["list", "delete404", "get"]


def test_restart_of_list_only_cleanup_requires_get_again_without_delete(tmp_path, monkeypatch):
    from runpod_lifecycle import api
    path = tmp_path / "claim.json"
    record = _claim_handle(state="cleanup_pending")
    record["release_quiescence"] = _quiescence(record)
    preparation.write_target_record(path, record)
    monkeypatch.setattr(api, "list_pods", lambda key: [])
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [_volume()])
    monkeypatch.setattr(api, "reconcile_pod_cleanup", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "terminate_pod", lambda *args: pytest.fail("absent pod cannot authorize DELETE"))
    monkeypatch.setattr(preparation, "read_exact_pod", lambda *args: (200, _pod()))

    async def forbidden(current):
        pytest.fail("saved exact quiescence must not replay")

    with pytest.raises(preparation.TargetObservationError):
        asyncio.run(preparation._release_exact_target_after_quiescence(
            SimpleNamespace(api_key="fake"), handle_path=path, attempt=record,
            quiesce_worker=forbidden, resume=True,
        ))
    pending = json.loads(path.read_text())
    assert pending["state"] == "cleanup_pending"
    monkeypatch.setattr(preparation, "read_exact_pod", lambda *args: (404, None))
    result = asyncio.run(preparation._release_exact_target_after_quiescence(
        SimpleNamespace(api_key="fake"), handle_path=path, attempt=pending,
        quiesce_worker=forbidden, resume=True,
    ))
    assert result["cleanup"]["status"] == "already_gone"
    assert result["cleanup"]["backup_volume_preserved"] is True


@pytest.mark.parametrize("change", [{"acquisition_mode": "attach"}, {"name": "another-broker"},
                                   {"request_name": "another-broker"}])
def test_foreign_or_attached_pod_is_never_terminated(tmp_path, monkeypatch, change):
    from runpod_lifecycle import api
    record = _claim_handle(**change)
    path = tmp_path / "claim.json"
    preparation.write_target_record(path, record)
    monkeypatch.setattr(api, "list_pods", lambda *args: pytest.fail("foreign inventory forbidden"))

    async def forbidden(current):
        pytest.fail("foreign task cannot be drained")

    with pytest.raises(preparation.TargetObservationError, match="journal's created"):
        asyncio.run(preparation._release_exact_target_after_quiescence(
            SimpleNamespace(api_key="fake"), handle_path=path, attempt=record, quiesce_worker=forbidden,
        ))


@pytest.mark.parametrize("status,body", [(404, None), (200, {"id": "pod-exact"}), (200, None),
                                       (401, {}), (500, None), (200, {"id": "foreign"})])
def test_status_preserving_exact_get_rejects_errors_and_null(monkeypatch, status, body):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(status, json=body)

    monkeypatch.setattr(httpx, "get", get)
    if status == 404 or status == 200 and body == {"id": "pod-exact"}:
        assert preparation.read_exact_pod("fake", "pod-exact") == (status, body)
    else:
        with pytest.raises(preparation.TargetObservationError):
            preparation.read_exact_pod("fake", "pod-exact")
    assert calls[0][0].endswith("/pod-exact")


def test_h3_provider_no_longer_collapses_error_to_absence(tmp_path, monkeypatch):
    from runpod_lifecycle import RunPodConfig, api
    monkeypatch.setattr(RunPodConfig, "from_env", lambda **kwargs: SimpleNamespace(api_key="fake"))
    monkeypatch.setattr(api, "get_pod_status", lambda *args: pytest.fail("lossy GET forbidden"))
    provider = LifecyclePodProvider(tmp_path)
    monkeypatch.setattr(preparation, "read_exact_pod", lambda *args: (200, None))
    with pytest.raises(RemotePlacementError):
        provider.pod_exists("pod-exact")
    monkeypatch.setattr(preparation, "read_exact_pod", lambda *args: (404, None))
    assert provider.pod_exists("pod-exact") is False
