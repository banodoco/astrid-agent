"""Focused ai-toolkit support module tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astrid.packs.training.shared.interfaces import ArtifactPullResult, RemoteExecResult, RunPodHandle
from astrid.packs.training.actions.training_run.ai_toolkit import register, review, stage, train
from astrid.packs.training.actions.training_run.ai_toolkit.train import Checkpoint


class FakeRemoteBackend:
    def __init__(self) -> None:
        self.exec_calls: list[dict[str, Any]] = []
        self.pull_calls: list[dict[str, Any]] = []

    def exec(self, handle: RunPodHandle, command: list[str], config: dict[str, Any]) -> RemoteExecResult:
        self.exec_calls.append({"handle": handle, "command": command, "config": dict(config)})
        return RemoteExecResult(exit_code=0, stdout="remote stdout", stderr="remote stderr", command=command, metadata={"config": dict(config)})

    def pull_artifacts(self, handle: RunPodHandle, remote_paths: list[str], local_dir: Path, config: dict[str, Any]) -> ArtifactPullResult:
        self.pull_calls.append({"handle": handle, "remote_paths": list(remote_paths), "local_dir": Path(local_dir), "config": dict(config)})
        local_dir.mkdir(parents=True, exist_ok=True)
        local_paths = []
        for remote_path in remote_paths:
            local_path = local_dir / Path(remote_path).name
            local_path.write_bytes(b"artifact")
            local_paths.append(local_path)
        return ArtifactPullResult(local_paths=local_paths, remote_paths=list(remote_paths), metadata={"config": dict(config)})


def _handle(tmp_path: Path) -> RunPodHandle:
    handle_path = tmp_path / "pod_handle.json"
    handle_path.write_text(json.dumps({"pod_id": "pod-123"}) + "\n", encoding="utf-8")
    return RunPodHandle("pod-123", "NVIDIA RTX 6000 Ada Generation", metadata={"handle_path": str(handle_path)})


def test_stage_preflights_and_uploads_with_mocked_backend(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    config = tmp_path / "config.yaml"
    manifest.write_text("{}\n", encoding="utf-8")
    config.write_text("config: {}\n", encoding="utf-8")
    remote = FakeRemoteBackend()

    result = stage.stage_training_inputs(
        remote,
        _handle(tmp_path),
        manifest_path=manifest,
        trainer_config_path=config,
        local_root=tmp_path / "stage",
        remote_root="/workspace/train",
        upload_mode="sftp_walk",
        excludes=[".git", "__pycache__"],
        produces_dir=tmp_path / "produces" / "stage",
        timeout=30,
    )

    call = remote.exec_calls[0]
    assert result.manifest_path == manifest.resolve()
    assert call["config"]["local_root"] == (tmp_path / "stage").resolve()
    assert call["config"]["remote_root"] == "/workspace/train"
    assert call["config"]["upload_mode"] == "sftp_walk"
    assert call["config"]["excludes"] == ".git,__pycache__"
    assert "mkdir -p /workspace/train/dataset" in call["config"]["remote_script"]


def test_training_mirrors_logs_and_parses_checkpoint_manifest(tmp_path: Path) -> None:
    remote = FakeRemoteBackend()
    result = train.run_training(
        remote,
        _handle(tmp_path),
        remote_config_path="/workspace/train/config.yaml",
        remote_output_dir="/workspace/train/output",
        log_path=tmp_path / "logs" / "train.log",
        produces_dir=tmp_path / "produces" / "train",
        timeout=120,
    )

    assert result.log_path.read_text(encoding="utf-8") == "remote stdout\nremote stderr\n"
    assert remote.exec_calls[0]["config"]["artifact_dir"] == "/workspace/train/output"
    manifest = tmp_path / "checkpoint_manifest.json"
    manifest.write_text(
        json.dumps({"checkpoints": [{"remote_path": "/workspace/out/lora-100.safetensors", "step": 100, "label": "step 100"}]}) + "\n",
        encoding="utf-8",
    )
    checkpoints = train.parse_checkpoint_manifest(manifest)
    assert checkpoints == [Checkpoint(remote_path="/workspace/out/lora-100.safetensors", step=100, label="step 100")]


def test_review_downloads_mp4s_and_writes_local_html_with_configurable_text(tmp_path: Path) -> None:
    remote = FakeRemoteBackend()
    result = review.generate_review_samples(
        remote,
        _handle(tmp_path),
        checkpoints=[Checkpoint(remote_path="/workspace/out/lora-100.safetensors", step=100, label="chosen checkpoint")],
        prompts=["prompt one", "prompt two"],
        local_dir=tmp_path / "review",
        title="City review",
        rubric=["style match", "motion quality"],
        remote_output_dir="/workspace/out",
        produces_dir=tmp_path / "produces" / "review",
    )

    html = result.index_path.read_text(encoding="utf-8")
    assert "City review" in html
    assert "style match" in html
    assert "prompt one" in html
    assert ".mp4" in html
    assert "/workspace" not in html
    assert all(sample.local_path.exists() for sample in result.samples)
    assert "ssh_key" not in remote.pull_calls[0]["config"]


def test_register_pulls_verifies_and_writes_registration_metadata(tmp_path: Path) -> None:
    remote = FakeRemoteBackend()
    result = register.register_checkpoint(
        remote,
        _handle(tmp_path),
        checkpoint_remote_path="/workspace/out/lora-final.safetensors",
        local_dir=tmp_path / "pulled",
        registry_dir=tmp_path / "registered",
        lora_id="city-night-v1",
        metadata={"checkpoint_label": "final"},
        produces_dir=tmp_path / "produces" / "register",
    )

    payload = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert result.pulled_checkpoint_path.exists()
    assert result.registered_lora_path.exists()
    assert result.registered_lora_path.suffix == ".safetensors"
    assert payload["lora_id"] == "city-night-v1"
    assert payload["source_checkpoint_remote_path"] == "/workspace/out/lora-final.safetensors"
    assert payload["metadata"]["checkpoint_label"] == "final"
    assert "ssh_key" not in remote.pull_calls[0]["config"]


def test_ai_toolkit_support_modules_do_not_hardcode_ssh_key_paths() -> None:
    package_root = Path("astrid/packs/training/actions/training_run/ai_toolkit")
    checked = list(package_root.glob("*.py"))
    assert checked
    forbidden = (".ssh", "id_rsa", "/Users/")
    for path in checked:
        text = path.read_text(encoding="utf-8")
        assert not any(value in text for value in forbidden), str(path)
