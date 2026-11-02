"""Release contracts with fake Runtime, HTTP, process and provider inventory."""
import asyncio
import copy
import json
import socket
from dataclasses import asdict, replace
from types import SimpleNamespace

import httpx
import pytest

from astrid.packs.runpod import worker_preparation as preparation
from astrid.packs.runpod.worker_owner import RunPodWorkerOwnerError
from tests.packs.runpod.test_worker_preparation import _claim_handle, _pod, _quiescence, _volume
from tests.packs.runpod.test_worker_release import release, release_case as release_case
from tests.packs.runpod.test_warm_session import warm_case as warm_case
from tests.test_runpod_managed_worker_session import _owner


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network forbidden"))


@pytest.mark.parametrize("get_case", ["absent", "present", "null", "auth", "error", "timeout"])
@pytest.mark.parametrize("list_case", ["absent", "stale", "null", "failed", "malformed", "duplicate"])
def test_lifecycle_delete404_http_matrix_requires_both_independent_observations(
    tmp_path, monkeypatch, get_case, list_case,
):
    from runpod_lifecycle import api
    record = _claim_handle()
    path = tmp_path / "claim.json"
    preparation.write_target_record(path, record)
    calls = []

    def listed(key):
        calls.append("list")
        if calls.count("list") == 1:
            return [_pod()]
        if list_case == "failed":
            raise TimeoutError("fake list timeout")
        return {"absent": [], "stale": [_pod()], "null": None,
                "malformed": [{}], "duplicate": [_pod(), _pod()]}[list_case]

    def delete(url, **kwargs):
        calls.append("delete404")
        assert url.endswith("/pod-exact")
        return httpx.Response(404)

    def get(url, **kwargs):
        calls.append("get")
        assert url.endswith("/pod-exact")
        if get_case == "timeout":
            raise httpx.TimeoutException("fake timeout")
        status, body = {"absent": (404, None), "present": (200, _pod()), "null": (200, None),
                        "auth": (401, {}), "error": (500, {})}[get_case]
        return httpx.Response(status, json=body)

    async def quiesce(current):
        return _quiescence(current)

    monkeypatch.setattr(api, "list_pods", listed)
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [_volume()])
    monkeypatch.setattr(api, "reconcile_pod_cleanup", lambda *a, **k: pytest.fail("DELETE404 returns early"))
    monkeypatch.setattr(httpx, "delete", delete)
    monkeypatch.setattr(httpx, "get", get)
    call = lambda: asyncio.run(preparation._release_exact_target_after_quiescence(
        SimpleNamespace(api_key="fake"), handle_path=path, attempt=record, quiesce_worker=quiesce,
    ))
    if get_case == "absent" and list_case == "absent":
        result = call()
        assert result["state"] == "cleanup_complete"
        assert result["cleanup"]["absence"] == {
            "provider_account_ref": record["provider_account_ref"], "pod_id": record["pod_id"],
            "exact_get_status": 404, "account_list_succeeded": True, "account_list_absent": True,
        }
    else:
        with pytest.raises(preparation.TargetObservationError):
            call()
        pending = json.loads(path.read_text())
        assert pending["state"] == "cleanup_pending" and pending["reconciliation_required"] is True
        assert "cleanup" not in pending
    assert calls[:3] == ["list", "delete404", "get"]


@pytest.mark.parametrize("lost", ["drain", "fence", "process", "credential", "delete", "receipt"])
def test_lost_release_response_resumes_only_saved_generation(release_case, monkeypatch, lost):
    from runpod_lifecycle import api
    c = release_case
    if lost == "drain":
        c.runtime.lose_finish = True
    elif lost == "fence":
        c.owner.lose_retire = True
    elif lost == "process":
        c.owner.lose_stop = True
    elif lost == "credential":
        remove = c.owner.remove_credential
        attempts = []
        async def lose_credential(process, identity):
            result = await remove(process, identity)
            attempts.append((process, copy.deepcopy(identity)))
            if len(attempts) == 1:
                raise TimeoutError("credential removed, response lost")
            return result
        monkeypatch.setattr(c.owner, "remove_credential", lose_credential)
    elif lost == "delete":
        terminate = api.terminate_pod
        def lose_delete(pod_id, key):
            terminate(pod_id, key)
            raise TimeoutError("pod deleted, response lost")
        monkeypatch.setattr(api, "terminate_pod", lose_delete)
    else:
        write = preparation.replace_target_record
        lost_once = []
        def lose_receipt(path, value, **kwargs):
            result = write(path, value, **kwargs)
            if value.get("state") == "cleanup_complete" and not lost_once:
                lost_once.append(True)
                raise TimeoutError("receipt persisted, response lost")
            return result
        monkeypatch.setattr(preparation, "replace_target_record", lose_receipt)
    with pytest.raises(TimeoutError):
        release(c)
    saved = json.loads(c.path.read_text())
    assert saved["state"] in {"release_pending", "cleanup_pending", "cleanup_complete"}
    calls_before = list(c.calls)
    original_digest = c.reference.digest
    c.reference.digest = lambda: "sha256:" + "f" * 64
    with pytest.raises(RunPodWorkerOwnerError, match="reference differs"):
        release(c, resume=True)
    assert c.calls == calls_before and json.loads(c.path.read_text()) == saved
    c.reference.digest = original_digest
    result = release(c, resume=True)
    assert result["state"] == "cleanup_complete"
    assert result["release_quiescence"]["task_id"] == c.reference.task_id
    assert result["release_quiescence"]["process_stop"]["birth_id"] == c.process.birth_id
    assert result["cleanup"]["absence"]["exact_get_status"] == 404
    assert result["cleanup"]["absence"]["account_list_absent"] is True
    assert c.calls.count("delete") == 1
    if lost == "credential":
        assert len(attempts) == 2 and attempts[0] == attempts[1]


@pytest.mark.parametrize("damage", ["missing", "get", "list", "account", "pod", "bool_status"])
def test_completed_receipt_without_independent_exact_absence_is_rejected(release_case, damage):
    c = release_case
    completed = release(c)
    if damage == "missing":
        del completed["cleanup"]["absence"]
    else:
        absence = completed["cleanup"]["absence"]
        if damage == "get":
            del absence["exact_get_status"]
        elif damage == "list":
            absence["account_list_succeeded"] = False
        elif damage == "account":
            absence["provider_account_ref"] = "foreign-account"
        elif damage == "pod":
            absence["pod_id"] = "foreign-pod"
        else:
            absence["exact_get_status"] = True
    preparation.write_target_record(c.path, completed)
    before = list(c.calls)
    with pytest.raises(preparation.TargetObservationError, match="independent exact GET404"):
        release(c, resume=True)
    assert c.calls == before


def test_lost_original_supervisor_response_resumes_same_session_only(release_case, monkeypatch):
    c = release_case
    owner, _transport, selected, _ = _owner()
    receipt = owner.start(selected)
    selected = replace(selected, session_ref="vibecomfy-h3", account_ref="runpod-test-account", pod_id="pod-exact")
    receipt = replace(receipt, session_ref="vibecomfy-h3", account_ref="runpod-test-account", pod_id="pod-exact")
    record = {**c.record, "managed_session": {
        "schema": "astrid.runpod.managed-session.v1", "claim_operation_id": c.record["operation_id"],
        "session_ref": selected.session_ref, "config_digest": selected.config_digest,
        "output_root": selected.output_root, "selection": asdict(selected), "receipt": receipt.as_dict(),
    }}
    preparation.write_target_record(c.path, record)
    c.reference.source_checkout = "/workspace/prepared/astrid"
    calls = []
    async def stop(program, args, **kwargs):
        assert json.loads(args[1]) == asdict(selected) and json.loads(args[2]) == receipt.as_dict()
        assert json.loads(c.path.read_text())["release_progress"]["credential_remove"] == {"removed": True}
        calls.append((program, args, kwargs))
        if len(calls) == 1:
            raise TimeoutError("original supervisor stopped; reply lost")
        return {"state": "stopped", "session_ref": selected.session_ref,
                "config_digest": selected.config_digest, "gpu_qualified": False,
                "supervisor_stop": "original_installation_owner",
                **{key: getattr(receipt, key) for key in ("daemon_pid", "daemon_birth_id", "comfy_pid", "comfy_birth_id")}}
    def fresh():
        preparer = c.fresh()
        preparer.session_owner = owner
        preparer.preparation_transport = SimpleNamespace(_run_json_program=stop)
        return preparer
    async def run():
        return await fresh().release(c.reference, runtime_owner=c.runtime, resume=True)
    with pytest.raises(RunPodWorkerOwnerError, match="supervisor stop is unresolved"):
        asyncio.run(fresh().release(c.reference, runtime_owner=c.runtime))
    assert "delete" not in c.calls and len(calls) == 1
    digest = c.reference.digest
    c.reference.digest = lambda: "sha256:" + "f" * 64
    with pytest.raises(RunPodWorkerOwnerError, match="reference differs"):
        asyncio.run(run())
    assert len(calls) == 1
    c.reference.digest = digest
    result = asyncio.run(run())
    assert result["state"] == "cleanup_complete"
    assert len(calls) == 2 and calls[0] == calls[1]
    assert c.calls == ["begin", "finish", "retire", "stop", "remove_credential", "delete"]
    assert result["release_quiescence"]["session_stop"]["daemon_birth_id"] == receipt.daemon_birth_id


def test_warm_owner_releases_only_its_journal_and_preserves_volumes(warm_case, tmp_path, monkeypatch):
    from runpod_lifecycle import api
    c = warm_case
    other = _pod(id="pod-other", name="other-broker", machineType="GPU-other", imageName="image-other",
                 networkVolumeId="vol-other")
    attached = _pod(id="pod-attached", name="attached", machineType="GPU-attached", imageName="image-attached",
                    networkVolumeId="vol-attached")
    foreign_volume = _volume(id="vol-other", name="other-volume")
    attached_volume = _volume(id="vol-attached", name="attached-volume")
    volumes = [_volume(), foreign_volume, attached_volume]
    c.case.pods.extend([other, attached])
    other_record = _claim_handle(operation_id="other-operation", request_name="other-broker", name="other-broker",
                                 pod_id="pod-other", gpu_type="GPU-other", worker_image="image-other",
                                 network_volume_id="vol-other", network_volume_name="other-volume")
    other_path = tmp_path / "other.json"
    preparation.write_target_record(other_path, other_record)
    before_other = other_path.read_bytes()
    before_volumes = copy.deepcopy(volumes)
    deleted = []
    def terminate(pod_id, key):
        assert pod_id == "pod-exact"
        deleted.append(pod_id)
        c.case.calls.append("delete")
        c.case.pods[:] = [p for p in c.case.pods if p["id"] != pod_id]
    monkeypatch.setattr(api, "terminate_pod", terminate)
    monkeypatch.setattr(api, "get_network_volumes", lambda key: copy.deepcopy(volumes))
    owner = c.fresh()
    owner.begin(c.case.reference)
    c.clock.advance(1000)
    result = c.read()
    assert result["state"] == "cleanup_complete"
    assert result["cleanup"]["backup_volume_id"] == "vol-persist"
    assert result["cleanup"]["backup_volume_preserved"] is True
    assert result["cleanup"]["absence"]["account_list_absent"] is True
    assert deleted == ["pod-exact"]
    assert c.case.pods == [other, attached]
    assert other_path.read_bytes() == before_other and volumes == before_volumes
    assert c.case.calls == ["begin", "finish", "retire", "stop", "remove_credential", "delete"]


@pytest.mark.parametrize("field,value", [
    ("name", "other-broker"), ("machineType", "GPU-other"), ("imageName", "image-other"),
    ("networkVolumeId", "vol-other"),
])
def test_other_journal_identity_on_selected_pod_blocks_delete(release_case, field, value):
    c = release_case
    c.pods[0][field] = value
    with pytest.raises(preparation.TargetObservationError):
        release(c)
    assert "delete" not in c.calls
    assert json.loads(c.path.read_text())["state"] == "cleanup_pending"


def test_attached_journal_cannot_stop_or_delete(warm_case):
    c = warm_case
    record = c.read()
    record["acquisition_mode"] = "attach"
    preparation.write_target_record(c.case.path, record)
    with pytest.raises((RunPodWorkerOwnerError, preparation.TargetObservationError)):
        c.fresh().begin(c.case.reference)
    with pytest.raises(preparation.TargetObservationError, match="journal's created"):
        release(c.case)
    assert c.case.calls == [] and not c.requests and len(c.case.pods) == 1


@pytest.mark.parametrize("lost", ["drain", "fence", "process", "credential", "delete", "receipt"])
def test_warm_owner_recovers_lost_release_for_saved_generation(warm_case, monkeypatch, lost):
    from runpod_lifecycle import api
    c = warm_case
    if lost == "drain":
        c.case.runtime.lose_finish = True
    elif lost == "fence":
        c.case.owner.lose_retire = True
    elif lost == "process":
        c.case.owner.lose_stop = True
    elif lost == "credential":
        remove = c.case.owner.remove_credential
        attempts = []
        async def lose_remove(process, identity):
            result = await remove(process, identity)
            attempts.append((process, copy.deepcopy(identity)))
            if len(attempts) == 1:
                raise TimeoutError("credential removed; response lost")
            return result
        monkeypatch.setattr(c.case.owner, "remove_credential", lose_remove)
    elif lost == "delete":
        terminate = api.terminate_pod
        def lose_delete(pod_id, key):
            terminate(pod_id, key)
            raise TimeoutError("pod deleted; response lost")
        monkeypatch.setattr(api, "terminate_pod", lose_delete)
    else:
        write = preparation.replace_target_record
        lost_once = []
        def lose_receipt(path, value, **kwargs):
            result = write(path, value, **kwargs)
            if value.get("state") == "cleanup_complete" and not lost_once:
                lost_once.append(True)
                raise TimeoutError("receipt persisted; response lost")
            return result
        monkeypatch.setattr(preparation, "replace_target_record", lose_receipt)
    owner = c.fresh()
    owner.begin(c.case.reference)
    c.clock.advance(1000)
    assert owner.last_error_type == "TimeoutError"
    saved = c.read()
    assert saved["state"] in {"release_pending", "cleanup_pending", "cleanup_complete"}
    before = list(c.case.calls)
    digest = c.case.reference.digest
    c.case.reference.digest = lambda: "sha256:" + "f" * 64
    with pytest.raises(RunPodWorkerOwnerError, match="invalid or foreign"):
        c.fresh().recover(c.case.reference)
    assert c.read() == saved and c.case.calls == before
    c.case.reference.digest = digest
    c.fresh().recover(c.case.reference)
    result = c.read()
    assert result["state"] == "cleanup_complete"
    assert result["release_quiescence"]["task_id"] == c.case.reference.task_id
    assert result["cleanup"]["absence"]["exact_get_status"] == 404
    assert result["cleanup"]["absence"]["account_list_succeeded"] is True
    assert c.case.calls.count("delete") == 1
    assert all(request["idempotency_key"] == "runpod-release:operation-1" for request in c.requests)


def test_warm_recovery_rejects_completed_receipt_without_absence(warm_case):
    c = warm_case
    c.fresh().begin(c.case.reference)
    c.clock.advance(1000)
    completed = c.read()
    del completed["cleanup"]["absence"]
    preparation.write_target_record(c.case.path, completed)
    before = list(c.case.calls)
    with pytest.raises(preparation.TargetObservationError, match="independent exact GET404"):
        c.fresh().recover(c.case.reference)
    assert c.case.calls == before


@pytest.mark.parametrize("field,value", [("provider_account_ref", "other-account"), ("pod_id", "pod-other")])
def test_foreign_runtime_target_cannot_stop_or_delete(release_case, field, value):
    c = release_case
    c.reference.effective_target = {**c.reference.effective_target, field: value}
    before = c.path.read_bytes()
    with pytest.raises(RunPodWorkerOwnerError, match="placement differs"):
        release(c)
    assert c.calls == [] and c.path.read_bytes() == before
