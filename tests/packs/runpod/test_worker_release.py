"""Joined P1 lock/journal and P3 drain/stop tests; no provider or GPU access."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from runtime_protocol import platform

from astrid.packs.runpod import worker_owner
from astrid.packs.runpod.worker_owner import RunPodRemoteWorkerPreparer, RunPodWorkerOwnerError
from astrid.packs.runpod.worker_preparation import (
    AcquisitionUnresolvedError,
    TargetObservationError,
    _release_exact_target_after_quiescence,
    write_target_record,
)
from astrid.packs.runpod.worker_process import WorkerBinding, WorkerProcessHandle
from tests.packs.runpod.test_worker_preparation import _claim_handle, _pod, _volume


@pytest.fixture
def release_case(tmp_path, monkeypatch):
    from runpod_lifecycle import api
    from astrid.packs.runpod import worker_preparation

    path = tmp_path / "claim.json"
    target = {"kind": "runpod", "pod_id": "pod-exact", "provider_account_ref": "runpod-test-account"}
    digest = "sha256:" + "a" * 64
    reference = SimpleNamespace(
        task_id="task-exact", run_id="run-exact", effective_target=target,
        runtime_instance_id="runtime-exact", runtime_epoch=3, executor_id="astrid-pack-host",
        deployment_binding=SimpleNamespace(digest=lambda: digest), digest=lambda: digest,
    )
    binding = WorkerBinding(
        account_ref=target["provider_account_ref"], pod_id=target["pod_id"], target=target,
        runtime_instance_id=reference.runtime_instance_id, runtime_epoch=3,
        runtime_session_id="session-exact", executor_id=reference.executor_id,
    )
    process = WorkerProcessHandle(binding, "incarnation-exact", "/private/owner.json",
                                  "/private/owner.lock", 4321, "birth-exact", 4321, 4321, 3)
    qualification = {
        "task_id": reference.task_id, "run_id": reference.run_id,
        "activation_id": "activation-exact", "deployment_digest": digest,
        "binding_digest": digest, "effective_target": target,
        "runtime_session_id": binding.runtime_session_id, "runtime_epoch": 3,
        "credential_actor": reference.executor_id, "executor_incarnation": process.incarnation,
        "evidence_digest": digest,
    }
    credential_reservation = {
        "path": "/workspace/runtime/credentials/tasks/task-exact/executor.token",
        "device": 2, "inode": 3, "mode": 0o600, "size": 0,
        "sha256": "sha256:" + hashlib.sha256(b"").hexdigest(),
    }
    record = _claim_handle(managed_worker={
        "schema": "astrid.runpod.managed-worker.v1", "state": "committed",
        "claim_operation_id": "operation-1", "pod_id": binding.pod_id,
        "provider_account_ref": binding.account_ref, "deployment_digest": digest,
        "process_handle": process.as_dict(), "activation_qualification": qualification,
        "observation_digest": digest, "executor_incarnation": process.incarnation,
        "credential_reservation": credential_reservation,
    })
    write_target_record(path, record)
    calls = []
    pods = [_pod()]

    def assert_locked():
        lock_path = path.with_name(f".{path.name}.owner.lock")
        fd = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                platform.lock_file(fd, blocking=False)
        finally:
            os.close(fd)

    class Runtime:
        finished = False
        lose_finish = False
        result = None

        def begin_remote_drain(self, task_id, actual):
            assert_locked()
            assert task_id == reference.task_id and actual == qualification
            assert not self.finished, "begin must not replay after credential removal"
            calls.append("begin")
            return {"state": "pending", "activation_id": qualification["activation_id"]}

        def finish_remote_drain(self, task_id, actual):
            assert_locked()
            assert task_id == reference.task_id and actual == qualification
            assert json.loads(path.read_text())["release_progress"]["drain_started"] is True
            calls.append("finish")
            if self.result is not None:
                return self.result
            self.finished = True
            if self.lose_finish:
                self.lose_finish = False
                raise TimeoutError("finish committed and credential was removed; reply lost")
            return {"state": "drained", "activation_id": qualification["activation_id"]}

    class ProcessOwner:
        stopped = False
        lose_stop = False
        lose_retire = False
        fence_override = None
        stop_override = None

        def _require_handle(self, actual):
            assert actual == process

        async def retire_for_release(self, actual, *, operation_id):
            assert_locked()
            assert actual == process and operation_id == record["operation_id"]
            assert runtime.finished
            calls.append("retire")
            if self.lose_retire:
                self.lose_retire = False
                raise TimeoutError("release fence persisted; reply lost")
            if self.fence_override is not None:
                return self.fence_override
            return {"retired": True, "operation_id": operation_id, "provider": "runpod",
                    "account_ref": binding.account_ref, "pod_id": binding.pod_id,
                    "incarnation": process.incarnation, "release_fence_digest": digest}

        async def stop(self, actual):
            assert_locked()
            assert actual == process
            assert json.loads(path.read_text())["release_progress"]["release_fence"]["retired"] is True
            calls.append("stop")
            if self.stop_override is not None:
                return self.stop_override
            already = self.stopped
            self.stopped = True
            if self.lose_stop:
                self.lose_stop = False
                raise TimeoutError("exact process exited but reply was lost")
            return {"stopped": True, "already_exited": already, "pid": process.pid,
                    "birth_id": process.birth_id, "incarnation": process.incarnation}

        async def remove_credential(self, actual, file_identity):
            assert_locked()
            assert actual == process and file_identity == credential_reservation
            assert self.stopped
            calls.append("remove_credential")
            return {"removed": True}

    runtime, process_owner = Runtime(), ProcessOwner()
    pod = SimpleNamespace(id=binding.pod_id, config=SimpleNamespace(api_key="fake-key"))

    def fresh_preparer():
        preparer = RunPodRemoteWorkerPreparer(
            pod, provider_account_ref=binding.account_ref, runtime_session_id=binding.runtime_session_id,
            claim_handle_path=path, journal_dir=tmp_path / "staging",
            staging_inputs={}, process_transport=SimpleNamespace(), preparation_transport=SimpleNamespace(),
        )
        monkeypatch.setattr(preparer, "_owner", lambda ref: process_owner)
        monkeypatch.setattr(preparer, "_stage", lambda ref: pytest.fail("release must not restage"))
        monkeypatch.setattr(preparer, "restore", lambda ref: pytest.fail("release must not restore readiness"))
        monkeypatch.setattr(preparer, "_claim_owner", lambda: pytest.fail("P1 already holds the only lock"))
        return preparer

    def terminate(pod_id, _key):
        assert_locked()
        assert runtime.finished and process_owner.stopped and pod_id == binding.pod_id
        assert json.loads(path.read_text())["release_quiescence"]["runtime_drain"]["state"] == "drained"
        calls.append("delete")
        pods.clear()

    monkeypatch.setattr(api, "list_pods", lambda key: list(pods))
    monkeypatch.setattr(api, "get_network_volumes", lambda key: [_volume()])
    monkeypatch.setattr(api, "terminate_pod", terminate)
    monkeypatch.setattr(worker_preparation, "read_exact_pod", lambda key, pod_id: (
        (200, next(p for p in pods if p["id"] == pod_id)) if any(p["id"] == pod_id for p in pods)
        else (404, None)
    ))
    def reconcile(pod_id, key, **kwargs):
        from runpod_lifecycle.errors import CleanupPendingError
        assert pod_id == binding.pod_id
        if pods:
            raise CleanupPendingError(pod_id, "same pod remains")
    monkeypatch.setattr(api, "reconcile_pod_cleanup", reconcile)
    return SimpleNamespace(
        path=path, reference=reference, qualification=qualification, process=process,
        record=record, runtime=runtime, owner=process_owner, calls=calls, pods=pods,
        fresh=fresh_preparer,
    )


def release(case, *, resume=False):
    return asyncio.run(case.fresh().release(case.reference, runtime_owner=case.runtime, resume=resume))


def test_joined_release_uses_one_lock_and_exact_runtime_response(release_case):
    case = release_case
    result = release(case)
    assert case.calls == ["begin", "finish", "retire", "stop", "remove_credential", "delete"]
    assert result["state"] == "cleanup_complete"
    evidence = result["release_quiescence"]
    assert evidence["drain"] == {"state": "complete", "task_id": case.reference.task_id,
                                  "activation_id": case.qualification["activation_id"]}
    assert evidence["runtime_drain"] == {"state": "drained", "activation_id": case.qualification["activation_id"]}
    assert result["release_progress"]["process_stop"] == evidence["process_stop"]
    assert evidence["credential_remove"] == {"removed": True}
    assert result["release_progress"]["credential_remove"] == {"removed": True}


def test_uncommitted_restart_cleanup_revokes_host_and_exact_reserved_secret(
    release_case, monkeypatch,
):
    case = release_case
    managed = dict(case.record["managed_worker"])
    managed["state"] = "enabled"
    from astrid.packs.runpod.worker_preparation import replace_target_record

    replace_target_record(case.path, {**case.record, "managed_worker": managed},
                         operation_id=case.record["operation_id"])

    async def stop(actual):
        assert actual == case.process
        case.owner.stopped = True
        case.calls.append("abort-stop")
        return {"stopped": True, "already_exited": False, "pid": case.process.pid,
                "birth_id": case.process.birth_id, "incarnation": case.process.incarnation}

    monkeypatch.setattr(case.owner, "stop", stop)
    preparer = RunPodRemoteWorkerPreparer(
        SimpleNamespace(id="pod-exact", config=SimpleNamespace(api_key="fake-key")),
        provider_account_ref="runpod-test-account", runtime_session_id="session-exact",
        claim_handle_path=case.path, journal_dir=case.path.parent / "staging",
        staging_inputs={}, process_transport=SimpleNamespace(), preparation_transport=SimpleNamespace(),
    )
    monkeypatch.setattr(preparer, "_owner", lambda _reference: case.owner)

    result = preparer.abort_uncommitted(
        case.reference, activation_id="activation-exact",
        runtime_revocation={"revoked": True},
    )

    assert result == {
        "stopped": True, "activation_id": "activation-exact",
        "incarnation": case.process.incarnation, "credential_removed": True,
    }
    saved = json.loads(case.path.read_text())["managed_worker"]
    assert saved["state"] == "stopped"
    assert saved["uncommitted_abort"]["runtime_revocation"] == {"revoked": True}
    assert saved["uncommitted_abort"]["credential_removed"] == {"removed": True}
    assert "credential_reservation" not in saved
    assert case.calls[-2:] == ["abort-stop", "remove_credential"]


def test_restart_after_lost_finish_skips_begin_and_preserves_checkpoint(release_case):
    case = release_case
    case.runtime.lose_finish = True
    with pytest.raises(TimeoutError, match="reply lost"):
        release(case)
    saved = json.loads(case.path.read_text())
    assert saved["state"] == "release_pending"
    assert saved["release_progress"]["drain_started"] is True
    assert "runtime_drain" not in saved["release_progress"]
    result = release(case, resume=True)
    assert result["state"] == "cleanup_complete"
    assert case.calls == ["begin", "finish", "finish", "retire", "stop", "remove_credential", "delete"]


def test_failed_begin_checkpoint_never_attempts_finish(release_case, monkeypatch):
    case = release_case
    checkpoint = worker_owner._checkpoint_release_progress
    def fail(path, expected, progress):
        raise OSError("journal write failed")
    monkeypatch.setattr(worker_owner, "_checkpoint_release_progress", fail)
    with pytest.raises(OSError, match="journal write"):
        release(case)
    assert case.calls == ["begin"]
    assert not case.runtime.finished
    monkeypatch.setattr(worker_owner, "_checkpoint_release_progress", checkpoint)
    assert release(case, resume=True)["state"] == "cleanup_complete"
    assert case.calls[:3] == ["begin", "begin", "finish"]


def test_restart_after_lost_stop_replays_only_exact_stop(release_case):
    case = release_case
    case.owner.lose_stop = True
    with pytest.raises(TimeoutError, match="process exited"):
        release(case)
    result = release(case, resume=True)
    assert result["release_quiescence"]["process_stop"]["already_exited"] is True
    assert case.calls == ["begin", "finish", "retire", "stop", "stop", "remove_credential", "delete"]


@pytest.mark.parametrize("response", [
    {"state": "pending", "activation_id": "activation-exact", "task_ids": ["task-exact"]},
    {"state": "complete", "activation_id": "activation-exact"},
    {"state": "drained", "activation_id": "foreign"},
    {"state": "drained", "activation_id": "activation-exact", "extra": True},
])
def test_only_exact_drained_response_can_stop_or_delete(release_case, response):
    case = release_case
    case.runtime.result = response
    with pytest.raises(RunPodWorkerOwnerError, match="remains active"):
        release(case)
    assert case.calls == ["begin", "finish"]
    assert case.pods and json.loads(case.path.read_text())["state"] == "release_pending"


def test_stale_same_state_claim_cannot_release_newer_managed_worker(release_case):
    case = release_case
    newer = copy.deepcopy(case.record)
    newer["managed_worker"]["state"] = "newer-checkpoint"
    write_target_record(case.path, newer)
    async def forbidden(record):
        pytest.fail("stale custody must not reach callback")
    with pytest.raises(AcquisitionUnresolvedError, match="changed"):
        asyncio.run(_release_exact_target_after_quiescence(
            SimpleNamespace(api_key="fake"), handle_path=case.path, attempt=case.record,
            quiesce_worker=forbidden,
        ))
    assert not case.calls


@pytest.mark.parametrize("field", ["pid", "birth_id", "incarnation"])
def test_foreign_stop_evidence_never_authorizes_delete(release_case, field):
    case = release_case
    stop = {"stopped": True, "pid": case.process.pid, "birth_id": case.process.birth_id,
            "incarnation": case.process.incarnation}
    stop[field] = 9999 if field == "pid" else "foreign"
    case.owner.stop_override = stop
    with pytest.raises(RunPodWorkerOwnerError, match="stop was not confirmed"):
        release(case)
    assert "delete" not in case.calls


def test_foreign_target_fence_never_authorizes_stop(release_case):
    case = release_case
    case.owner.fence_override = {"retired": True, "operation_id": "different-claim"}
    with pytest.raises(RunPodWorkerOwnerError, match="release fence"):
        release(case)
    assert case.calls == ["begin", "finish", "retire"]


def test_saved_quiescence_must_match_persisted_process_on_resume(release_case):
    case = release_case
    result = release(case)
    result["state"] = "cleanup_pending"
    result["managed_worker"]["process_handle"]["birth_id"] = "new-birth"
    write_target_record(case.path, result)
    calls = list(case.calls)
    with pytest.raises(TargetObservationError, match="persisted worker generation"):
        release(case, resume=True)
    assert case.calls == calls


def test_lost_release_fence_reply_resumes_without_restarting_drain(release_case):
    case = release_case
    case.owner.lose_retire = True
    with pytest.raises(TimeoutError, match="fence persisted"):
        release(case)
    assert release(case, resume=True)["state"] == "cleanup_complete"
    assert case.calls == ["begin", "finish", "retire", "retire", "stop", "remove_credential", "delete"]


def test_crash_after_stop_checkpoint_reuses_exact_stop_evidence(release_case, monkeypatch):
    from astrid.packs.runpod import worker_preparation
    case = release_case
    replace = worker_preparation.replace_target_record
    failed = False
    def fail_quiescence(path, value, **kwargs):
        nonlocal failed
        if "release_quiescence" in value and not failed:
            failed = True
            raise OSError("quiescence write interrupted after process-stop checkpoint")
        return replace(path, value, **kwargs)
    monkeypatch.setattr(worker_preparation, "replace_target_record", fail_quiescence)
    with pytest.raises(OSError, match="quiescence write interrupted"):
        release(case)
    assert json.loads(case.path.read_text())["release_progress"]["process_stop"]["stopped"] is True
    assert release(case, resume=True)["state"] == "cleanup_complete"
    assert case.calls == ["begin", "finish", "retire", "stop", "remove_credential", "delete"]


def test_missing_target_fence_capability_fails_before_runtime_drain(release_case):
    case = release_case
    case.owner.retire_for_release = None
    with pytest.raises(RunPodWorkerOwnerError, match="fencing is unavailable"):
        release(case)
    assert not case.calls


def test_resume_rejects_reference_for_another_deployment(release_case):
    case = release_case
    case.runtime.lose_finish = True
    with pytest.raises(TimeoutError):
        release(case)
    case.reference.digest = lambda: "sha256:" + "f" * 64
    before = list(case.calls)
    with pytest.raises(RunPodWorkerOwnerError, match="reference differs"):
        release(case, resume=True)
    assert case.calls == before


def test_lost_final_release_reply_replays_terminal_receipt_without_side_effects(release_case, monkeypatch):
    from runpod_lifecycle import api
    case = release_case
    completed = release(case)
    before = list(case.calls)
    monkeypatch.setattr(api, "list_pods", lambda key: pytest.fail("completed release must replay its receipt"))
    monkeypatch.setattr(api, "get_network_volumes", lambda key: pytest.fail("completed release must replay its receipt"))
    assert release(case, resume=True) == completed
    assert case.calls == before
