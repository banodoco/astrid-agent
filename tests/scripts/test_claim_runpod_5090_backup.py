from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
import runpod_lifecycle
from runpod_lifecycle import AllocationUnknown, LaunchFailure, PostCreateHookFailure

from astrid.packs.runpod import worker_preparation
from scripts import claim_runpod_5090_backup as claim

_TEST_API_KEY = "rpa_test_secret_must_not_persist"
_VOLUME = {"id": "vol-backup", "name": "backup", "size": 250}


def test_obsolete_direct_worker_activation_entrypoint_is_retired() -> None:
    entrypoint = Path(__file__).resolve().parents[2] / "scripts" / "activate_runpod_worker_claim.py"

    assert not entrypoint.exists()


def _args(handle_path: Path, *extra: str):
    provider_ref = ["--provider-account-ref", "runpod-test-account"]
    if "--provider-account-ref" in extra:
        provider_ref = []
    return claim._parser().parse_args(
        ["--handle-path", str(handle_path), *provider_ref, *extra]
    )


def _offline_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        claim.RunPodConfig,
        "from_env",
        staticmethod(
            lambda **overrides: claim.RunPodConfig(
                api_key=_TEST_API_KEY,
                **overrides,
            )
        ),
    )

    async def require_existing_volume(config: Any, name: str) -> dict[str, Any]:
        assert name == "backup"
        assert config.storage_name == "backup"
        return dict(_VOLUME)

    monkeypatch.setattr(claim, "_require_existing_volume", require_existing_volume)

    async def observe_exact(api_key: str, contract: worker_preparation.RunPodTargetContract):
        assert api_key == _TEST_API_KEY
        return worker_preparation.RunPodTargetObservation(
            pod_id=contract.pod_id,
            pod_name=contract.expected_pod_name or "existing-pod",
            desired_status="RUNNING",
            actual_status="RUNNING",
            gpu_type=contract.gpu_type,
            worker_image=contract.worker_image,
            network_volume_id=contract.network_volume_id,
            network_volume_name=contract.network_volume_name or "backup",
            network_volume_size_gb=contract.network_volume_size_gb,
            datacenter_id=None,
            observed_at="2026-10-03T00:00:00Z",
        )

    monkeypatch.setattr(worker_preparation, "observe_exact_target", observe_exact)


def _write_terminal_cleanup(handle_path: Path, handle: dict[str, Any]) -> Path:
    receipt_path = handle_path.parent.parent / "receipts" / "terminal-cleanup.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(
            {
                "claim_handle_digest": claim._canonical_digest(handle),
                "pod_id": handle["pod_id"],
                "cleanup_status": "complete",
                "cleanup": {
                    "pod_id": handle["pod_id"],
                    "status": "terminated",
                    "backup_volume_id": handle["network_volume_id"],
                    "backup_volume_preserved": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return receipt_path


def test_defaults_match_the_canonical_twelve_hour_claim() -> None:
    args = claim._parser().parse_args([])

    assert args.handle_path == str(claim.DEFAULT_HANDLE_PATH)
    assert args.max_wait_seconds == 43200
    assert args.poll_seconds == 120
    assert args.lifecycle_policy == "leave_running"
    assert args.storage_name == "backup"
    assert args.provider_account_ref is None
    assert args.worker_image == claim.DEFAULT_WORKER_IMAGE
    assert args.template_id is None
    assert args.allowed_cuda_versions == "13.0"


def test_release_preflight_is_cpu_only_and_never_queries_cuda() -> None:
    class Pod:
        command = ""

        async def exec_ssh(self, command: str, *, timeout: int):
            self.command = command
            assert timeout == 120
            return 0, "python=3.12.8", ""

    pod = Pod()
    result = asyncio.run(claim._preflight_release(pod, "/workspace/release"))

    assert result["probe"] == "python=3.12.8"
    assert "torch" not in pod.command.casefold()
    assert "cuda" not in pod.command.casefold()
    assert "nvidia-smi" not in pod.command


def test_allocation_unknown_persists_secret_free_marker_and_never_opens_another_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle_path = tmp_path / "claim-handle.json"
    args = _args(
        handle_path,
        "--max-wait-seconds",
        "2",
    )
    _offline_volume(monkeypatch)
    launches: list[str] = []

    async def ambiguous_launch(config: Any, *, name: str, **kwargs: Any) -> Any:
        launches.append(name)
        if len(launches) > 1:
            raise AssertionError("allocation uncertainty opened another capacity window")
        marker = json.loads(handle_path.read_text(encoding="utf-8"))
        assert marker["schema_version"] == "astrid.runpod.allocation-attempt.v1"
        assert marker["state"] == "allocation_pending"
        assert marker["request_name"] == name
        assert marker["storage_name"] == "backup"
        assert marker["network_volume_id"] == "vol-backup"
        assert marker["lifecycle"] == {"mode": "leave_running"}
        assert config.attach_only is True
        assert config.disk_size_gb == 0
        assert config.storage_name == "backup"
        assert config.storage_volumes == ("backup",)
        assert config.worker_image == claim.DEFAULT_WORKER_IMAGE
        assert config.template_id is None
        assert config.allowed_cuda_versions == ("13.0",)
        assert 1 <= kwargs["max_wait_sec"] <= 2
        assert kwargs["retry_interval_sec"] == 120
        raise AllocationUnknown(name, claim.DEFAULT_GPU, 72, "backup", "vol-backup")

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", ambiguous_launch)

    with pytest.raises(AllocationUnknown):
        asyncio.run(claim._claim(args))

    assert len(launches) == 1
    marker_text = handle_path.read_text(encoding="utf-8")
    marker = json.loads(marker_text)
    assert marker["state"] == "allocation_unknown"
    assert marker["reconciliation_required"] is True
    assert marker["provider_account_ref"] == "runpod-test-account"
    assert marker["request_name"] == launches[0]
    assert marker["gpu_type_attempted"] == claim.DEFAULT_GPU
    assert marker["ram_tier_attempted"] == 72
    assert marker["storage_name_attempted"] == "backup"
    assert marker["storage_volume_id_attempted"] == "vol-backup"
    assert "pod_id" not in marker
    assert _TEST_API_KEY not in marker_text


def test_definite_capacity_retry_is_journaled_before_the_next_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runpod_lifecycle import api

    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError

    handle_path = tmp_path / "claim-handle.json"
    args = _args(handle_path, "--max-wait-seconds", "30")
    _offline_volume(monkeypatch)
    transitions: list[tuple[str, int, bool]] = []
    real_replace = worker_preparation.replace_target_record

    def recording_replace(path: Path, value: dict[str, Any], *, operation_id: str) -> None:
        transitions.append(
            (
                str(value.get("state")),
                int(value.get("create_attempts", 0)),
                value.get("safe_to_retry") is True,
            )
        )
        real_replace(path, value, operation_id=operation_id)

    monkeypatch.setattr(worker_preparation, "replace_target_record", recording_replace)
    calls = 0

    async def create(config: Any, *, name: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LaunchFailure("definite capacity rejection")
        marker = json.loads(handle_path.read_text(encoding="utf-8"))
        assert marker["state"] == "allocation_pending"
        assert marker["create_attempts"] == 2
        assert marker["safe_to_retry"] is False
        raise AllocationUnknown(name, claim.DEFAULT_GPU, 72, "backup", "vol-backup")

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", create)

    with pytest.raises(AllocationUnknown):
        asyncio.run(claim._claim(args))

    marker = json.loads(handle_path.read_text(encoding="utf-8"))
    assert calls == 2
    assert marker["state"] == "allocation_unknown"
    assert marker["create_attempts"] == 2
    retry_ready = transitions.index(("capacity_retry_ready", 1, True))
    second_pending = transitions.index(("allocation_pending", 2, False))
    assert retry_ready < second_pending
    monkeypatch.setattr(api, "list_pods", lambda _api_key: [])
    with pytest.raises(AcquisitionUnresolvedError, match="0 exact-name matches"):
        asyncio.run(claim._claim(_args(handle_path, "--resume")))
    assert calls == 2


def test_resume_reconciles_empty_account_reads_then_adopts_original_without_second_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runpod_lifecycle import api

    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError

    handle_path = tmp_path / "claim-handle.json"
    args = _args(handle_path)
    _offline_volume(monkeypatch)
    creates = 0

    async def ambiguous_create(config: Any, *, name: str, **kwargs: Any) -> Any:
        nonlocal creates
        creates += 1
        raise AllocationUnknown(name, claim.DEFAULT_GPU, 72, "backup", "vol-backup")

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", ambiguous_create)
    with pytest.raises(AllocationUnknown):
        asyncio.run(claim._claim(args))

    original = json.loads(handle_path.read_text(encoding="utf-8"))
    operation_id = original["operation_id"]
    request_name = original["request_name"]
    resume_args = _args(handle_path, "--resume")
    account_reads: list[list[dict[str, Any]]] = [
        [],
        [],
        [{"id": "pod-original", "name": request_name}],
    ]
    monkeypatch.setattr(api, "list_pods", lambda _api_key: account_reads.pop(0))

    class Pod:
        id = "pod-original"
        name = request_name
        _gpu_type = claim.DEFAULT_GPU
        _storage_name = "backup"
        _storage_volume = "vol-backup"

        async def wait_ready(self, *, timeout: int) -> None:
            assert timeout == claim.DEFAULT_READY_TIMEOUT_SECONDS

        async def _ensure_ssh_details(self) -> dict[str, Any]:
            return {"ip": "203.0.113.42", "port": 22022}

    async def attach(pod_id: str, config: Any, *, name: str) -> Pod:
        assert (pod_id, name) == ("pod-original", request_name)
        return Pod()

    async def preflight(_pod: Any, release_root: str) -> dict[str, str]:
        return {"release_root": release_root, "probe": "offline ready"}

    monkeypatch.setattr(runpod_lifecycle, "get_pod", attach)
    monkeypatch.setattr(claim, "_preflight_release", preflight)

    for _ in range(2):
        with pytest.raises(AcquisitionUnresolvedError, match="0 exact-name matches"):
            asyncio.run(claim._claim(resume_args))
    result = asyncio.run(claim._claim(resume_args))

    assert creates == 1
    assert account_reads == []
    assert result["operation_id"] == operation_id
    assert result["request_name"] == request_name
    assert result["pod_id"] == "pod-original"
    assert result["state"] == "claimed"


def test_resume_after_intent_only_keeps_zero_match_unresolved_without_first_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runpod_lifecycle import api

    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError

    handle_path = tmp_path / "claim-handle.json"
    _offline_volume(monkeypatch)
    args = _args(handle_path)
    attempt = claim._start_allocation_attempt(
        handle_path,
        args=args,
        volume=dict(_VOLUME),
    )
    assert attempt["state"] == "allocation_ready"
    assert attempt["create_attempts"] == 0
    reads: list[str] = []
    creates: list[str] = []
    monkeypatch.setattr(api, "list_pods", lambda _api_key: reads.append("list") or [])

    async def forbidden_create(config: Any, *, name: str, **kwargs: Any) -> Any:
        creates.append(name)
        raise AssertionError("an empty listing cannot authorize a first create on resume")

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", forbidden_create)

    with pytest.raises(AcquisitionUnresolvedError, match="0 exact-name matches"):
        asyncio.run(claim._claim(_args(handle_path, "--resume")))

    marker = json.loads(handle_path.read_text(encoding="utf-8"))
    assert reads == ["list"]
    assert creates == []
    assert marker["state"] == "allocation_unknown"
    assert marker["reconciliation_required"] is True
    assert marker["create_attempts"] == 0


def test_resume_blocks_duplicate_exact_names_before_contract_filtering_or_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runpod_lifecycle import api

    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError

    handle_path = tmp_path / "claim-handle.json"
    _offline_volume(monkeypatch)
    args = _args(handle_path)

    async def ambiguous_create(config: Any, *, name: str, **kwargs: Any) -> Any:
        raise AllocationUnknown(name, claim.DEFAULT_GPU, 72, "backup", "vol-backup")

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", ambiguous_create)
    with pytest.raises(AllocationUnknown):
        asyncio.run(claim._claim(args))
    attempt = json.loads(handle_path.read_text(encoding="utf-8"))
    request_name = attempt["request_name"]
    calls: list[str] = []

    monkeypatch.setattr(
        api,
        "list_pods",
        lambda _api_key: calls.append("list")
        or [
            {"id": "pod-candidate-valid", "name": request_name},
            {"id": "pod-candidate-other", "name": request_name},
        ],
    )

    async def forbidden_attach(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("ambiguous names must not be attached")

    async def forbidden_create(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("ambiguous names must not create a replacement")

    monkeypatch.setattr(runpod_lifecycle, "get_pod", forbidden_attach)
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", forbidden_create)

    with pytest.raises(AcquisitionUnresolvedError, match="2 exact-name matches"):
        asyncio.run(claim._claim(_args(handle_path, "--resume")))

    updated = json.loads(handle_path.read_text(encoding="utf-8"))
    assert calls == ["list"]
    assert updated["state"] == "allocation_unknown"
    assert updated["last_reconciliation_candidate_ids"] == [
        "pod-candidate-valid",
        "pod-candidate-other",
    ]


def test_resume_with_known_pod_id_never_substitutes_same_name_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runpod_lifecycle import api

    from astrid.packs.runpod.worker_preparation import (
        TargetObservationError,
        inspect_target_records,
    )

    handle_path = tmp_path / "claim-handle.json"
    _offline_volume(monkeypatch)
    args = _args(handle_path)
    attempt = claim._start_allocation_attempt(
        handle_path,
        args=args,
        volume=dict(_VOLUME),
    )
    request_name = str(attempt["request_name"])
    known_id = "pod-known-but-missing"
    claim._replace_allocation_attempt(
        handle_path,
        {
            **attempt,
            "state": "allocated_unverified",
            "pod_id": known_id,
            "name": request_name,
            "reconciliation_required": True,
        },
        operation_id=str(attempt["operation_id"]),
    )
    observed_ids: list[str] = []

    async def observe_same_name_other_id(
        _api_key: str, contract: worker_preparation.RunPodTargetContract
    ) -> Any:
        observed_ids.append(contract.pod_id)
        return inspect_target_records(
            contract,
            pods=[
                {
                    "id": "pod-same-name-other-id",
                    "name": request_name,
                    "desiredStatus": "RUNNING",
                    "actualStatus": "RUNNING",
                    "machineType": claim.DEFAULT_GPU,
                    "imageName": claim.DEFAULT_WORKER_IMAGE,
                    "networkVolumeId": "vol-backup",
                }
            ],
            volumes=[dict(_VOLUME)],
        )

    monkeypatch.setattr(
        worker_preparation, "observe_exact_target", observe_same_name_other_id
    )
    monkeypatch.setattr(
        api,
        "list_pods",
        lambda _api_key: (_ for _ in ()).throw(
            AssertionError("known-ID recovery must not search by name")
        ),
    )

    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("known-ID mismatch must not attach or allocate a substitute")

    monkeypatch.setattr(runpod_lifecycle, "get_pod", forbidden)
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", forbidden)

    with pytest.raises(TargetObservationError, match="not uniquely visible"):
        asyncio.run(claim._claim(_args(handle_path, "--resume")))

    marker = json.loads(handle_path.read_text(encoding="utf-8"))
    assert observed_ids == [known_id]
    assert marker["pod_id"] == known_id
    assert marker["state"] == "allocated_unverified"
    assert marker["reconciliation_required"] is True


def test_owner_rejects_concurrent_claim_journal_before_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from runtime_protocol import platform

    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError

    handle_path = tmp_path / "claim-handle.json"
    _offline_volume(monkeypatch)
    args = _args(handle_path)

    async def ambiguous_create(config: Any, *, name: str, **kwargs: Any) -> Any:
        raise AllocationUnknown(name, claim.DEFAULT_GPU, 72, "backup", "vol-backup")

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", ambiguous_create)
    with pytest.raises(AllocationUnknown):
        asyncio.run(claim._claim(args))

    marker = json.loads(handle_path.read_text(encoding="utf-8"))
    lock_path = handle_path.with_name(f".{handle_path.name}.owner.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    platform.lock_file(fd, blocking=False)
    try:
        with pytest.raises(AcquisitionUnresolvedError, match="another process currently owns"):
            asyncio.run(claim._claim(_args(handle_path, "--resume")))
    finally:
        platform.unlock_file(fd)
        os.close(fd)

    assert json.loads(handle_path.read_text(encoding="utf-8"))["operation_id"] == marker[
        "operation_id"
    ]


def test_known_pod_launch_failure_is_preserved_and_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle_path = tmp_path / "claim-handle.json"
    args = _args(handle_path, "--max-wait-seconds", "10")
    _offline_volume(monkeypatch)
    launches: list[str] = []

    async def launch_with_known_pod(config: Any, *, name: str, **kwargs: Any) -> Any:
        launches.append(name)
        assert json.loads(handle_path.read_text(encoding="utf-8"))["request_name"] == name
        raise PostCreateHookFailure("pod-created-before-hook-error", RuntimeError("state hook failed"))

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", launch_with_known_pod)

    with pytest.raises(PostCreateHookFailure):
        asyncio.run(claim._claim(args))

    assert launches and len(launches) == 1
    marker_text = handle_path.read_text(encoding="utf-8")
    marker = json.loads(marker_text)
    assert marker["state"] == "allocated_unverified"
    assert marker["provider_account_ref"] == "runpod-test-account"
    assert marker["pod_id"] == "pod-created-before-hook-error"
    assert marker["reconciliation_required"] is True
    assert marker["request_name"] == launches[0]
    assert marker["failure_type"] == "PostCreateHookFailure"
    assert _TEST_API_KEY not in marker_text


def test_success_atomically_replaces_marker_with_claim_handle_and_leaves_pod_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle_path = tmp_path / "claim-handle.json"
    args = _args(handle_path)
    _offline_volume(monkeypatch)
    replace_destinations: list[Path] = []
    real_replace = os.replace

    def recording_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        replace_destinations.append(Path(destination))
        real_replace(source, destination)

    monkeypatch.setattr(claim.os, "replace", recording_replace)

    class Pod:
        id = "pod-claimed"
        name = "astrid-claim-5090-backup-test"
        _gpu_type = claim.DEFAULT_GPU
        _storage_name = "backup"
        _storage_volume = "vol-backup"

        def __init__(self) -> None:
            self.terminate_calls = 0

        async def wait_ready(self, *, timeout: int) -> None:
            assert timeout == claim.DEFAULT_READY_TIMEOUT_SECONDS

        async def _ensure_ssh_details(self) -> dict[str, Any]:
            return {"ip": "203.0.113.10", "port": 22022}

        async def terminate(self) -> None:
            self.terminate_calls += 1

    pod = Pod()

    async def successful_launch(config: Any, *, name: str, **kwargs: Any) -> Pod:
        marker = json.loads(handle_path.read_text(encoding="utf-8"))
        assert marker["state"] == "allocation_pending"
        assert marker["request_name"] == name
        assert config.attach_only is True
        assert config.disk_size_gb == 0
        assert kwargs == {"max_wait_sec": 3600, "retry_interval_sec": 120}
        pod.name = name
        return pod

    async def successful_preflight(pod_arg: Any, release_root: str) -> dict[str, str]:
        assert pod_arg is pod
        assert release_root == claim.DEFAULT_RELEASE_ROOT
        return {
            "release_root": release_root,
            "release_python": f"{release_root}/runtime/venv/bin/python",
            "release_launcher": f"{release_root}/runtime/launch-comfy.sh",
            "probe": "cpu fake",
        }

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", successful_launch)
    monkeypatch.setattr(claim, "_preflight_release", successful_preflight)

    result = asyncio.run(claim._claim(args))

    final_text = handle_path.read_text(encoding="utf-8")
    final = json.loads(final_text)
    assert final == result
    assert final["schema_version"] == "astrid.runpod.claim.v1"
    assert final["state"] == "claimed"
    assert final["pod_id"] == "pod-claimed"
    assert final["storage_name"] == "backup"
    assert final["network_volume_id"] == "vol-backup"
    assert final["network_volume_size_gb"] == 250
    assert final["attach_only"] is True
    assert final["container_disk_gb"] == 200
    assert final["worker_image"] == claim.DEFAULT_WORKER_IMAGE
    assert final["template_id"] is None
    assert final["allowed_cuda_versions"] == ["13.0"]
    assert final["lifecycle"] == {"mode": "leave_running"}
    assert final["acquisition_mode"] == "allocate"
    assert final["release_authorized"] is True
    assert final["provider_observation"]["pod_id"] == "pod-claimed"
    assert final["api_key_ref"] == "RUNPOD_API_KEY"
    assert final["reconciliation_required"] is False
    assert final["request_name"].startswith("astrid-claim-5090-backup-")
    assert final["operation_id"][:12] in final["request_name"]
    assert final["handle_path"] == str(handle_path.resolve())
    assert pod.terminate_calls == 0
    assert replace_destinations[-1] == handle_path.resolve()
    assert len(replace_destinations) == 4
    assert not list(tmp_path.glob(".claim-handle.json.*.tmp"))
    assert _TEST_API_KEY not in final_text


def test_allocated_failure_is_recorded_and_leave_running_does_not_retry_or_terminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle_path = tmp_path / "claim-handle.json"
    args = _args(handle_path)
    _offline_volume(monkeypatch)
    launches = 0

    class Pod:
        id = "pod-unverified"
        name = "astrid-claim-5090-backup-unverified"

        def __init__(self) -> None:
            self.terminate_calls = 0

        async def wait_ready(self, *, timeout: int) -> None:
            raise RuntimeError("offline readiness failure")

        async def terminate(self) -> None:
            self.terminate_calls += 1

    pod = Pod()

    async def allocated_launch(config: Any, *, name: str, **kwargs: Any) -> Pod:
        nonlocal launches
        launches += 1
        pod.name = name
        return pod

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", allocated_launch)

    with pytest.raises(RuntimeError, match="offline readiness failure"):
        asyncio.run(claim._claim(args))

    marker = json.loads(handle_path.read_text(encoding="utf-8"))
    assert launches == 1
    assert pod.terminate_calls == 0
    assert marker["state"] == "allocated_unverified"
    assert marker["pod_id"] == "pod-unverified"
    assert marker["failure_phase"] == "readiness"
    assert marker["failure_type"] == "RuntimeError"
    assert marker["lifecycle"] == {"mode": "leave_running"}
    assert marker["reconciliation_required"] is True


def test_existing_custody_blocks_before_any_provider_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle_path = tmp_path / "claim-handle.json"
    handle_path.write_text(
        json.dumps(
            {
                "schema_version": "astrid.runpod.allocation-attempt.v1",
                "state": "allocation_unknown",
                "operation_id": "existing-operation",
                "request_name": "existing-request",
            }
        ),
        encoding="utf-8",
    )
    args = _args(handle_path)

    async def forbidden_volume(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("provider volume lookup must not run")

    async def forbidden_launch(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("provider launch must not run")

    monkeypatch.setattr(claim, "_require_existing_volume", forbidden_volume)
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", forbidden_launch)

    with pytest.raises(RuntimeError, match="requires reconciliation; rerun this command with --resume"):
        asyncio.run(claim._claim(args))


def test_stale_terminal_claim_is_archived_before_one_new_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle_path = tmp_path / "runpod" / "claim-handle.json"
    handle_path.parent.mkdir(parents=True)
    old_handle = {
        "schema_version": "astrid.runpod.claim.v1",
        "pod_id": "pod-terminated",
        "network_volume_id": "vol-backup",
        "storage_name": "backup",
    }
    old_bytes = (json.dumps(old_handle, indent=2, sort_keys=True) + "\n").encode()
    handle_path.write_bytes(old_bytes)
    receipt_path = _write_terminal_cleanup(handle_path, old_handle)
    args = _args(handle_path)
    _offline_volume(monkeypatch)
    launches = 0

    class Pod:
        id = "pod-new"
        name = "astrid-claim-5090-backup-new"
        _gpu_type = claim.DEFAULT_GPU
        _storage_name = "backup"
        _storage_volume = "vol-backup"

        async def wait_ready(self, *, timeout: int) -> None:
            assert timeout == claim.DEFAULT_READY_TIMEOUT_SECONDS

        async def _ensure_ssh_details(self) -> dict[str, Any]:
            return {"ip": "203.0.113.20", "port": 22022}

    async def launch(config: Any, *, name: str, **kwargs: Any) -> Pod:
        nonlocal launches
        launches += 1
        assert launches == 1
        marker = json.loads(handle_path.read_text(encoding="utf-8"))
        assert marker["state"] == "allocation_pending"
        assert marker["previous_claim"]["pod_id"] == "pod-terminated"
        pod = Pod()
        pod.name = name
        return pod

    async def preflight(_pod: Any, release_root: str) -> dict[str, str]:
        return {"release_root": release_root, "probe": "offline ready"}

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", launch)
    monkeypatch.setattr(claim, "_preflight_release", preflight)

    result = asyncio.run(claim._claim(args))

    previous = result["previous_claim"]
    archive_path = Path(previous["archive_path"])
    assert result["pod_id"] == "pod-new"
    assert launches == 1
    assert previous["cleanup_receipt_path"] == str(receipt_path.resolve())
    assert previous["handle_digest"] == claim._canonical_digest(old_handle)
    assert archive_path.read_bytes() == old_bytes
    assert json.loads(handle_path.read_text(encoding="utf-8")) == result


def test_exact_attach_uses_provider_validation_without_allocating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle_path = tmp_path / "claim-handle.json"
    args = _args(handle_path, "--pod-id", "pod-existing")
    _offline_volume(monkeypatch)
    launches: list[str] = []

    class Pod:
        id = "pod-existing"
        name = "existing-pod"
        _gpu_type = claim.DEFAULT_GPU
        _storage_name = "backup"
        _storage_volume = "vol-backup"

        async def wait_ready(self, *, timeout: int) -> None:
            assert timeout == claim.DEFAULT_READY_TIMEOUT_SECONDS

        async def _ensure_ssh_details(self) -> dict[str, Any]:
            return {"ip": "203.0.113.30", "port": 22022}

    async def attach(pod_id: str, config: Any, *, name: str) -> Pod:
        assert pod_id == "pod-existing"
        assert name == "existing-pod"
        return Pod()

    async def forbidden_launch(*_args: Any, **_kwargs: Any) -> Any:
        launches.append("unexpected")
        raise AssertionError("exact attach must not allocate")

    async def preflight(_pod: Any, release_root: str) -> dict[str, str]:
        return {"release_root": release_root, "probe": "offline ready"}

    monkeypatch.setattr(runpod_lifecycle, "get_pod", attach)
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", forbidden_launch)
    monkeypatch.setattr(claim, "_preflight_release", preflight)

    result = asyncio.run(claim._claim(args))

    assert launches == []
    assert result["pod_id"] == "pod-existing"
    assert result["acquisition_mode"] == "attach"
    assert result["release_authorized"] is False
    assert result["provider_observation"]["network_volume_id"] == "vol-backup"
    assert result["api_key_ref"] == "RUNPOD_API_KEY"


def test_wrong_account_attach_stops_before_ssh_or_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astrid.packs.runpod.worker_preparation import TargetObservationError

    handle_path = tmp_path / "claim-handle.json"
    args = _args(handle_path, "--pod-id", "pod-not-visible")
    _offline_volume(monkeypatch)
    monkeypatch.setattr(
        worker_preparation,
        "observe_exact_target",
        lambda *_args, **_kwargs: _raise(TargetObservationError("pod is not in this account")),
    )

    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("mismatching exact attach must stop before SSH or allocation")

    def _raise(exc: Exception) -> Any:
        raise exc

    monkeypatch.setattr(runpod_lifecycle, "get_pod", forbidden)
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", forbidden)

    with pytest.raises(TargetObservationError, match="not in this account"):
        asyncio.run(claim._claim(args))

    marker = json.loads(handle_path.read_text(encoding="utf-8"))
    assert marker["state"] == "attached_unverified"
    assert marker["requested_pod_id"] == "pod-not-visible"
    assert marker["reconciliation_required"] is True


def test_attach_rejects_lifecycle_object_for_a_different_pod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astrid.packs.runpod.worker_preparation import TargetObservationError

    handle_path = tmp_path / "claim-handle.json"
    args = _args(handle_path, "--pod-id", "pod-requested")
    _offline_volume(monkeypatch)

    class WrongPod:
        id = "pod-other"
        name = "other-pod"

    async def wrong_pod(*_args: Any, **_kwargs: Any) -> WrongPod:
        return WrongPod()

    monkeypatch.setattr(runpod_lifecycle, "get_pod", wrong_pod)

    with pytest.raises(TargetObservationError, match="returned pod identity"):
        asyncio.run(claim._claim(args))

    marker = json.loads(handle_path.read_text(encoding="utf-8"))
    assert marker["state"] == "attached_unverified"
    assert marker["requested_pod_id"] == "pod-requested"
    assert marker["reconciliation_required"] is True


def test_active_claim_handle_refuses_before_any_provider_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle_path = tmp_path / "runpod" / "claim-handle.json"
    handle_path.parent.mkdir(parents=True)
    handle_path.write_text(
        json.dumps(
            {
                "schema_version": "astrid.runpod.claim.v1",
                "state": "claimed",
                "pod_id": "pod-active",
                "network_volume_id": "vol-backup",
            }
        ),
        encoding="utf-8",
    )

    async def forbidden_volume(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("provider volume lookup must not run")

    async def forbidden_launch(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("provider launch must not run")

    monkeypatch.setattr(claim, "_require_existing_volume", forbidden_volume)
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", forbidden_launch)

    with pytest.raises(RuntimeError, match="already identifies pod pod-active"):
        asyncio.run(claim._claim(_args(handle_path)))
