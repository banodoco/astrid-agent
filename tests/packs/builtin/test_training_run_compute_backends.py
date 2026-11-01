"""Training-run compute and remote backend registry contracts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from astrid.packs.training.shared.interfaces import RunPodHandle
from astrid.packs.training.actions.training_run.compute_backends import (
    BackendExecutionError,
    BackendRegistryError,
    RunPodComputeBackend,
    RunPodRemoteExecutionBackend,
    get_compute_backend,
    get_remote_execution_backend,
)


def _arg_value(argv: Sequence[str], flag: str) -> str:
    return str(argv[argv.index(flag) + 1])


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        call = list(argv)
        self.calls.append(call)
        produces = Path(_arg_value(call, "--produces-dir"))
        produces.mkdir(parents=True, exist_ok=True)
        module_index = next(
            index
            for index, arg in enumerate(call)
            if arg.startswith("astrid.packs.runpod.actions.") and arg.endswith(".run")
        )
        command = call[module_index + 1]
        assert call[module_index] == f"astrid.packs.runpod.actions.{command}.run"
        if command == "provision":
            (produces / "pod_handle.json").write_text(
                json.dumps(
                    {
                        "pod_id": "pod-123",
                        "gpu_type": "NVIDIA RTX 6000 Ada Generation",
                        "ui_url": "https://pod.example",
                        "config_snapshot": {"ports": _arg_value(call, "--ports")},
                    }
                ) + "\n",
                encoding="utf-8",
            )
        elif command == "exec":
            (produces / "exec_result.json").write_text(
                json.dumps({"returncode": 0, "stdout": "ok", "stderr": "", "artifact_dir": str(produces / "artifact_dir")}) + "\n",
                encoding="utf-8",
            )
        elif command == "pull":
            local_dir = Path(_arg_value(call, "--local-dir"))
            (produces / "artifact_pull.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "artifacts": [
                            {
                                "remote_path": "/workspace/output/checkpoint.safetensors",
                                "local_path": str(local_dir / "checkpoint.safetensors"),
                                "exists": True,
                            }
                        ],
                    }
                ) + "\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(call, 0, stdout="", stderr="")


def test_backend_registries_resolve_runpod_and_list_available_on_invalid_id() -> None:
    assert get_compute_backend("runpod").backend_id == "runpod"
    assert get_remote_execution_backend("runpod").backend_id == "runpod"

    with pytest.raises(BackendRegistryError, match="available: runpod"):
        get_compute_backend("local")
    with pytest.raises(BackendRegistryError, match="available: runpod"):
        get_remote_execution_backend("ssh")


def test_runpod_compute_preserves_provision_arguments_and_estimates_cost(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = RunPodComputeBackend(repo_root=tmp_path, runner=runner)

    handle = backend.provision(
        {
            "produces_dir": tmp_path / "provision",
            "gpu_type": "NVIDIA RTX 6000 Ada Generation",
            "storage_name": "astrid-storage",
            "max_runtime_seconds": 3600,
            "name_prefix": "astrid-train",
            "image": "ostris/aitoolkit:latest",
            "container_disk_gb": 200,
            "datacenter_id": "EU-RO-1",
            "ports": "8675/http,22/tcp",
            "max_gpu_hours": 2,
            "max_runpod_spend_usd": 10,
        }
    )

    call = runner.calls[0]
    assert handle.pod_id == "pod-123"
    assert _arg_value(call, "--gpu-type") == "NVIDIA RTX 6000 Ada Generation"
    assert _arg_value(call, "--storage-name") == "astrid-storage"
    assert _arg_value(call, "--max-runtime-seconds") == "3600"
    assert _arg_value(call, "--name-prefix") == "astrid-train"
    assert _arg_value(call, "--image") == "ostris/aitoolkit:latest"
    assert _arg_value(call, "--container-disk-gb") == "200"
    assert _arg_value(call, "--datacenter-id") == "EU-RO-1"
    assert _arg_value(call, "--ports") == "8675/http,22/tcp"

    estimate = backend.estimate_cost({"gpu_type": "NVIDIA RTX 6000 Ada Generation", "max_gpu_hours": 2})
    assert estimate.backend == "runpod"
    assert estimate.gpu_hours == 2
    assert estimate.estimated_cost_usd == 1.58
    assert estimate.details["pricing_source"] == "pinned_training_run_rates"


def test_runpod_compute_storage_required_requires_name_before_command(tmp_path: Path) -> None:
    backend = RunPodComputeBackend(repo_root=tmp_path, runner=RecordingRunner())

    with pytest.raises(BackendExecutionError, match="ensure-storage"):
        backend.provision(
            {
                "produces_dir": tmp_path / "provision",
                "storage_required": True,
                "gpu_type": "NVIDIA RTX 6000 Ada Generation",
            }
        )


def test_runpod_compute_storage_required_threads_require_storage_flag(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = RunPodComputeBackend(repo_root=tmp_path, runner=runner)

    backend.provision(
        {
            "produces_dir": tmp_path / "provision",
            "storage_required": True,
            "storage_name": "astrid-storage",
            "ports": "8675/http,22/tcp",
        }
    )

    call = runner.calls[0]
    assert "--require-storage" in call
    assert _arg_value(call, "--storage-name") == "astrid-storage"


def test_runpod_remote_exec_and_pull_preserve_existing_argument_shapes(tmp_path: Path) -> None:
    runner = RecordingRunner()
    remote = RunPodRemoteExecutionBackend(repo_root=tmp_path, runner=runner)
    handle_path = tmp_path / "provision" / "pod_handle.json"
    handle_path.parent.mkdir()
    handle_path.write_text(json.dumps({"pod_id": "pod-123"}) + "\n", encoding="utf-8")
    handle = RunPodHandle("pod-123", "NVIDIA RTX 6000 Ada Generation", metadata={"handle_path": str(handle_path)})

    result = remote.exec(
        handle,
        ["python", "train.py"],
        {
            "produces_dir": tmp_path / "exec",
            "local_root": tmp_path / "local",
            "remote_root": "/workspace/train",
            "upload_mode": "sftp_walk",
            "excludes": ".git,__pycache__",
            "timeout": 120,
        },
    )
    exec_call = runner.calls[0]
    assert result.exit_code == 0
    assert _arg_value(exec_call, "--pod-handle") == str(handle_path)
    assert _arg_value(exec_call, "--local-root") == str(tmp_path / "local")
    assert _arg_value(exec_call, "--remote-root") == "/workspace/train"
    assert _arg_value(exec_call, "--remote-script") == "python train.py"
    assert _arg_value(exec_call, "--upload-mode") == "sftp_walk"
    assert _arg_value(exec_call, "--excludes") == ".git,__pycache__"
    assert _arg_value(exec_call, "--timeout") == "120"

    pull = remote.pull_artifacts(
        handle,
        ["/workspace/output/checkpoint.safetensors"],
        tmp_path / "artifacts",
        {"produces_dir": tmp_path / "pull", "ssh_key": tmp_path / "id_rsa"},
    )
    pull_call = runner.calls[1]
    assert _arg_value(pull_call, "--pod-handle") == str(handle_path)
    assert _arg_value(pull_call, "--remote-path") == "/workspace/output/checkpoint.safetensors"
    assert _arg_value(pull_call, "--local-dir") == str(tmp_path / "artifacts")
    assert _arg_value(pull_call, "--ssh-key") == str(tmp_path / "id_rsa")
    assert pull.local_paths == [tmp_path / "artifacts" / "checkpoint.safetensors"]


def test_runpod_teardown_is_idempotent_for_missing_handle_or_gone_pod(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = RunPodComputeBackend(repo_root=tmp_path, runner=runner)
    missing = RunPodHandle("pod-123", "NVIDIA RTX 6000 Ada Generation", metadata={"handle_path": str(tmp_path / "missing.json")})
    backend.teardown(missing)
    assert runner.calls == []

    handle_path = tmp_path / "pod_handle.json"
    handle_path.write_text(json.dumps({"pod_id": "pod-123"}) + "\n", encoding="utf-8")

    def gone_runner(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 2, stdout="", stderr="pod not found")

    backend = RunPodComputeBackend(repo_root=tmp_path, runner=gone_runner)
    backend.teardown(RunPodHandle("pod-123", "NVIDIA RTX 6000 Ada Generation", metadata={"handle_path": str(handle_path)}))
