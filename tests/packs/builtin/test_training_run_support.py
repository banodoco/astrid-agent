"""Generic training-run config and manifest support contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.packs.training.shared.interfaces import ArtifactPullResult, CostEstimate, ProviderCapabilities, RemoteExecResult, RunPodHandle
import astrid.packs.training.actions.training_run.run as training_run_module
from astrid.packs.training.actions.training_run.config import (
    TrainingRunBudgetError,
    TrainingRunConfigError,
    TrainingRunSecretError,
    TrainingRunSpendConfirmationError,
    load_training_run_config,
    preflight_budget,
    preflight_secrets,
    preflight_training_run,
)
from astrid.packs.training.actions.training_run.manifest import (
    compatibility_manifest_path,
    normalize_ai_toolkit_manifest,
    seed_from_dataset_run,
)
from astrid.packs.training.actions.training_run.run import main as training_run_main
from astrid.packs.training.actions.training_run.state import (
    make_initial_state,
    read_last_run_state,
    record_failure,
    record_pod_ready,
    write_last_run_state,
)


def _clip_fixture(root: Path) -> tuple[Path, Path]:
    clips = root / "clips"
    clips.mkdir()
    clip = clips / "clip_001.mp4"
    caption = clips / "clip_001.caption.json"
    clip.write_bytes(b"mp4")
    caption.write_text(json.dumps({"text": "caption"}) + "\n", encoding="utf-8")
    return clip, caption


def _flat_manifest(path: Path, clip: Path, caption: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "clip_id": "clip_001",
                        "clip_file": str(clip),
                        "path": str(clip),
                        "caption_file": str(caption),
                        "bucket": "test",
                    }
                ]
            }
        ) + "\n",
        encoding="utf-8",
    )
    return path


def _training_config(
    path: Path,
    *,
    manifest_path: str = "manifest.json",
    output_run_dir: str = "train-run",
    extra: dict | None = None,
    compute: dict | None = None,
) -> Path:
    payload = {
        "schema_version": 1,
        "trainer_id": "ai-toolkit-ltx",
        "manifest_path": manifest_path,
        "compute": {
            "backend": "runpod",
            "max_gpu_hours": 2,
            "max_runpod_spend_usd": 10,
        },
        "secrets": {"required_env": ["RUNPOD_API_KEY"]},
        "base_model": "base.safetensors",
        "lora_config": {
            "lora_id": "demo",
            "trigger_word": "demo style",
            "prompt_text": "A demo training prompt.",
            "rank": 8,
            "alpha": 8,
            "steps": 100,
            "learning_rate": 0.0001,
            "seed": 1,
            "width": 512,
            "height": 512,
            "num_frames": 49,
            "fps": 24,
            "batch_size": 1,
            "gradient_accumulation_steps": 1,
            "save_every": 50,
            "sample_every": 50,
        },
        "checkpoint": {
            "sample_prompts": ["demo style, test sample"],
            "review_labels": ["style"],
        },
        "output": {"run_dir": output_run_dir},
        "extensions": {"local": {"ok": True}},
    }
    if compute:
        payload["compute"].update(compute)
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def test_secret_preflight_reports_missing_env_in_dry_run_and_fails_live() -> None:
    config = {"secrets": {"required_env": ["RUNPOD_API_KEY", "HF_TOKEN"]}}

    dry = preflight_secrets(config, dry_run=True, env={})
    assert dry.missing_env == ("RUNPOD_API_KEY", "HF_TOKEN")

    with pytest.raises(TrainingRunSecretError):
        preflight_secrets(config, dry_run=False, env={"RUNPOD_API_KEY": "present"})

    live = preflight_secrets(config, dry_run=False, env={"RUNPOD_API_KEY": "present", "HF_TOKEN": "present"})
    assert live.missing_env == ()


def test_training_config_rejects_unknown_fields_but_allows_extensions_and_resolves_relative_paths(tmp_path: Path) -> None:
    config_path = _training_config(tmp_path / "config.json", manifest_path="dataset/manifest.json", output_run_dir="runs/train")

    parsed = load_training_run_config(config_path)

    assert parsed.manifest_path == tmp_path / "dataset" / "manifest.json"
    assert parsed.run_dir == tmp_path / "runs" / "train"
    assert parsed.data["manifest_path"] == str(tmp_path / "dataset" / "manifest.json")
    assert parsed.data["extensions"]["local"]["ok"] is True

    unknown_path = _training_config(tmp_path / "unknown.json", extra={"surprise": True})
    with pytest.raises(TrainingRunConfigError, match="Additional properties"):
        load_training_run_config(unknown_path)


def test_training_config_allows_runtime_remote_execution_options(tmp_path: Path) -> None:
    config_path = _training_config(
        tmp_path / "config.json",
        compute={
            "name_prefix": "astrid-training",
            "datacenter_id": "EU-RO-1",
            "upload_mode": "sftp_walk",
            "excludes": [".git", "__pycache__"],
            "ssh_key": "keys/runpod",
            "stage_timeout_seconds": 120,
            "training_timeout_seconds": 3600,
            "review_timeout_seconds": 900,
        },
        extra={"output": {"run_dir": "runs/train", "remote_root": "/workspace/train", "remote_output_dir": "/workspace/out"}},
    )

    parsed = load_training_run_config(config_path)

    assert parsed.data["compute"]["upload_mode"] == "sftp_walk"
    assert parsed.data["compute"]["excludes"] == [".git", "__pycache__"]
    assert parsed.data["compute"]["ssh_key"] == "keys/runpod"
    assert parsed.data["output"]["remote_root"] == "/workspace/train"
    assert parsed.data["output"]["remote_output_dir"] == "/workspace/out"


def test_training_config_enforces_supported_backend_trainer_and_required_budget_fields(tmp_path: Path) -> None:
    unsupported_trainer = _training_config(tmp_path / "trainer.json", extra={"trainer_id": "custom"})
    with pytest.raises(TrainingRunConfigError, match="trainer_id"):
        load_training_run_config(unsupported_trainer)

    unsupported_backend = _training_config(tmp_path / "backend.json", compute={"backend": "local"})
    with pytest.raises(TrainingRunConfigError, match="compute.backend"):
        load_training_run_config(unsupported_backend)

    missing_budget = _training_config(tmp_path / "budget.json")
    payload = json.loads(missing_budget.read_text(encoding="utf-8"))
    payload["compute"].pop("max_runpod_spend_usd")
    missing_budget.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(TrainingRunConfigError, match="max_runpod_spend_usd"):
        load_training_run_config(missing_budget)


def test_training_config_requires_storage_name_when_runpod_storage_required(tmp_path: Path) -> None:
    config_path = _training_config(
        tmp_path / "storage-required.json",
        compute={"storage_required": True},
    )

    with pytest.raises(TrainingRunConfigError, match="ensure-storage"):
        load_training_run_config(config_path)

    configured_path = _training_config(
        tmp_path / "storage-configured.json",
        compute={"storage_required": True, "storage_name": "astrid-storage"},
    )
    parsed = load_training_run_config(configured_path)
    assert parsed.data["compute"]["storage_required"] is True
    assert parsed.data["compute"]["storage_name"] == "astrid-storage"


def test_budget_preflight_requires_positive_caps_and_live_spend_confirmation() -> None:
    config = {
        "trainer_id": "ai-toolkit-ltx",
        "compute": {
            "backend": "runpod",
            "max_gpu_hours": 2,
            "max_runpod_spend_usd": 10,
            "require_spend_confirmation": True,
        },
    }

    dry = preflight_budget(config, dry_run=True, spend_confirmed=False)
    assert dry.max_gpu_hours == 2
    assert dry.max_runpod_spend_usd == 10

    with pytest.raises(TrainingRunSpendConfirmationError):
        preflight_budget(config, dry_run=False, spend_confirmed=False)

    live = preflight_budget(config, dry_run=False, spend_confirmed=True)
    assert live.spend_confirmed is True

    zero_budget = {"trainer_id": "ai-toolkit-ltx", "compute": {**config["compute"], "max_gpu_hours": 0}}
    with pytest.raises(TrainingRunBudgetError):
        preflight_budget(zero_budget, dry_run=True, spend_confirmed=False)


def test_training_run_preflight_combines_budget_and_declared_secrets_before_live() -> None:
    config = {
        "trainer_id": "ai-toolkit-ltx",
        "secrets": {"required_env": ["RUNPOD_API_KEY"]},
        "compute": {
            "backend": "runpod",
            "max_gpu_hours": 2,
            "max_runpod_spend_usd": 10,
            "require_spend_confirmation": True,
        },
    }

    dry = preflight_training_run(config, dry_run=True, spend_confirmed=False, env={})
    assert dry.secrets.missing_env == ("RUNPOD_API_KEY",)
    assert dry.budget.require_spend_confirmation is True

    with pytest.raises(TrainingRunSecretError):
        preflight_training_run(config, dry_run=False, spend_confirmed=True, env={})


def test_training_run_main_dry_run_writes_state_and_live_preflight_fails_before_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip, caption = _clip_fixture(tmp_path)
    manifest = _flat_manifest(tmp_path / "manifest.json", clip, caption)
    run_dir = tmp_path / "train-run"
    config_path = _training_config(
        tmp_path / "config.json",
        manifest_path=manifest.name,
        output_run_dir=run_dir.name,
    )

    dry_rc = training_run_main(["--config", str(config_path), "--dry-run", "--json"])
    assert dry_rc == 0
    dry_state = json.loads((run_dir / "last_run.json").read_text(encoding="utf-8"))
    assert dry_state["phase"] == "preflight_ready"
    assert dry_state["secrets"]["missing_env"] == ["RUNPOD_API_KEY"]
    assert dry_state["budget"]["max_runpod_spend_usd"] == 10

    live_run_dir = tmp_path / "live-run"
    live_config = _training_config(
        tmp_path / "live-config.json",
        manifest_path=manifest.name,
        output_run_dir=live_run_dir.name,
    )
    monkeypatch.setenv("RUNPOD_API_KEY", "present")

    live_rc = training_run_main(["--config", str(live_config)])

    assert live_rc == 2
    assert not (live_run_dir / "last_run.json").exists()


def test_training_run_dry_run_and_smoke_write_local_plan_artifacts_without_backend_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class NoProvisionCompute:
        backend_id = "runpod"

        def provision(self, config: dict) -> None:
            raise AssertionError("dry-run must not provision")

        def teardown(self, handle: object) -> None:
            raise AssertionError("dry-run must not teardown")

        def estimate_cost(self, config: dict) -> CostEstimate:
            return CostEstimate(gpu_hours=2, estimated_cost_usd=1.58, backend="runpod", details={"source": "test"})

    class NoRemoteCalls:
        backend_id = "runpod"
        capabilities = ProviderCapabilities(
            backend="runpod",
            supports_exec=True,
            supports_artifact_pull=True,
            supports_artifact_push=True,
            supports_cost_estimate=True,
            metadata={"test": True},
        )

        def exec(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("dry-run must not execute remotely")

        def pull_artifacts(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("dry-run must not pull artifacts")

    monkeypatch.setattr(training_run_module, "get_compute_backend", lambda backend_id: NoProvisionCompute())
    monkeypatch.setattr(training_run_module, "get_remote_execution_backend", lambda backend_id: NoRemoteCalls())

    clip, caption = _clip_fixture(tmp_path)
    manifest = _flat_manifest(tmp_path / "manifest.json", clip, caption)
    run_dir = tmp_path / "dry-run"
    config_path = _training_config(
        tmp_path / "config.json",
        manifest_path=manifest.name,
        output_run_dir=run_dir.name,
    )

    dry_rc = training_run_main(["--config", str(config_path), "--manifest", str(manifest), "--out", str(run_dir), "--dry-run", "--json"])

    assert dry_rc == 0
    state = json.loads((run_dir / "last_run.json").read_text(encoding="utf-8"))
    planned_cost = json.loads((run_dir / "planned_cost.json").read_text(encoding="utf-8"))
    trainer_config = run_dir / "trainer" / "ai-toolkit-ltx" / "config.yaml"
    normalized_manifest = run_dir / "manifests" / "ai-toolkit-ltx" / "manifest.json"
    assert normalized_manifest.is_file()
    assert trainer_config.is_file()
    assert planned_cost["estimate"]["estimated_cost_usd"] == 1.58
    assert planned_cost["within_budget"] is True
    assert state["artifacts"]["trainer_config_path"] == str(trainer_config)
    assert state["compute"]["remote_capabilities"]["supports_artifact_pull"] is True

    smoke_dir = tmp_path / "smoke-run"
    smoke_rc = training_run_main(["--config", str(config_path), "--out", str(smoke_dir), "--smoke", "--json"])
    assert smoke_rc == 0
    smoke_state = json.loads((smoke_dir / "last_run.json").read_text(encoding="utf-8"))
    assert smoke_state["mode"] == "smoke"
    assert (smoke_dir / "planned_cost.json").is_file()


def test_training_run_dry_run_rejects_estimated_cost_above_budget_before_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class ExpensiveCompute:
        backend_id = "runpod"

        def provision(self, config: dict) -> None:
            raise AssertionError("dry-run must not provision")

        def teardown(self, handle: object) -> None:
            raise AssertionError("dry-run must not teardown")

        def estimate_cost(self, config: dict) -> CostEstimate:
            return CostEstimate(gpu_hours=99, estimated_cost_usd=99, backend="runpod")

    class NoRemoteCalls:
        backend_id = "runpod"
        capabilities = ProviderCapabilities(backend="runpod")

        def exec(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("dry-run must not execute remotely")

        def pull_artifacts(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("dry-run must not pull artifacts")

    monkeypatch.setattr(training_run_module, "get_compute_backend", lambda backend_id: ExpensiveCompute())
    monkeypatch.setattr(training_run_module, "get_remote_execution_backend", lambda backend_id: NoRemoteCalls())

    clip, caption = _clip_fixture(tmp_path)
    manifest = _flat_manifest(tmp_path / "manifest.json", clip, caption)
    run_dir = tmp_path / "over-budget"
    config_path = _training_config(tmp_path / "config.json", manifest_path=manifest.name, output_run_dir=run_dir.name)

    rc = training_run_main(["--config", str(config_path), "--dry-run", "--json"])

    assert rc == 2
    assert not (run_dir / "last_run.json").exists()
    assert not (run_dir / "planned_cost.json").exists()


class MockLiveCompute:
    backend_id = "runpod"

    def __init__(self, tmp_path: Path, *, include_handle_path: bool = True) -> None:
        self.tmp_path = tmp_path
        self.include_handle_path = include_handle_path
        self.provision_calls: list[dict] = []
        self.teardown_calls: list[RunPodHandle] = []

    def provision(self, config: dict) -> RunPodHandle:
        self.provision_calls.append(dict(config))
        metadata = {}
        if self.include_handle_path:
            handle_path = self.tmp_path / "pod_handle.json"
            handle_path.write_text(json.dumps({"pod_id": "pod-live"}) + "\n", encoding="utf-8")
            metadata["handle_path"] = str(handle_path)
        return RunPodHandle("pod-live", "NVIDIA RTX 6000 Ada Generation", metadata=metadata)

    def teardown(self, handle: RunPodHandle) -> None:
        self.teardown_calls.append(handle)

    def estimate_cost(self, config: dict) -> CostEstimate:
        return CostEstimate(gpu_hours=2, estimated_cost_usd=1.58, backend="runpod")


class MockLiveRemote:
    backend_id = "runpod"
    capabilities = ProviderCapabilities(
        backend="runpod",
        supports_exec=True,
        supports_artifact_pull=True,
        supports_artifact_push=True,
        supports_cost_estimate=True,
    )

    def __init__(self, *, fail_exec_index: int | None = None) -> None:
        self.fail_exec_index = fail_exec_index
        self.exec_calls: list[dict] = []
        self.pull_calls: list[dict] = []

    def exec(self, handle: RunPodHandle, command: list[str], config: dict) -> RemoteExecResult:
        self.exec_calls.append({"handle": handle, "command": list(command), "config": dict(config)})
        if self.fail_exec_index == len(self.exec_calls):
            return RemoteExecResult(exit_code=1, stdout="", stderr="remote failed", command=command)
        return RemoteExecResult(exit_code=0, stdout=f"ok-{len(self.exec_calls)}", stderr="", command=command)

    def pull_artifacts(self, handle: RunPodHandle, remote_paths: list[str], local_dir: Path, config: dict) -> ArtifactPullResult:
        self.pull_calls.append({"handle": handle, "remote_paths": list(remote_paths), "local_dir": local_dir, "config": dict(config)})
        local_dir.mkdir(parents=True, exist_ok=True)
        local_paths = []
        for remote_path in remote_paths:
            local_path = local_dir / Path(remote_path).name
            local_path.write_bytes(b"mp4")
            local_paths.append(local_path)
        return ArtifactPullResult(local_paths=local_paths, remote_paths=list(remote_paths), metadata={})


def test_training_run_live_mocked_pauses_at_review_gate_without_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compute = MockLiveCompute(tmp_path)
    remote = MockLiveRemote()
    monkeypatch.setattr(training_run_module, "get_compute_backend", lambda backend_id: compute)
    monkeypatch.setattr(training_run_module, "get_remote_execution_backend", lambda backend_id: remote)
    monkeypatch.setenv("RUNPOD_API_KEY", "present")
    clip, caption = _clip_fixture(tmp_path)
    manifest = _flat_manifest(tmp_path / "manifest.json", clip, caption)
    run_dir = tmp_path / "live-run"
    config_path = _training_config(tmp_path / "config.json", manifest_path=manifest.name, output_run_dir=run_dir.name)

    rc = training_run_main(["--config", str(config_path), "--out", str(run_dir), "--confirm-spend", "--json"])

    assert rc == 0
    assert len(compute.provision_calls) == 1
    assert compute.teardown_calls == []
    assert len(remote.exec_calls) == 3
    assert len(remote.pull_calls) == 1
    state = json.loads((run_dir / "last_run.json").read_text(encoding="utf-8"))
    assert state["status"] == "PAUSED"
    assert state["phase"] == "review_ready"
    assert state["phase_history"] == ["preflight_ready", "provisioning", "pod_ready", "staging", "training", "pulling_artifacts", "review_ready"]
    assert state["pod"]["id"] == "pod-live"
    assert state["recoverability"]["teardown_guard"]["required"] is True
    assert "checkpoint_review" == state["review"]["human_gate"]
    assert Path(state["artifacts"]["checkpoint_manifest_path"]).is_file()
    assert Path(state["artifacts"]["training_log_path"]).is_file()
    review_html = Path(state["artifacts"]["review_index_path"]).read_text(encoding="utf-8")
    assert ".mp4" in review_html
    assert "/workspace" not in review_html


def test_training_run_live_training_failure_preserves_pod_for_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compute = MockLiveCompute(tmp_path)
    remote = MockLiveRemote(fail_exec_index=2)
    monkeypatch.setattr(training_run_module, "get_compute_backend", lambda backend_id: compute)
    monkeypatch.setattr(training_run_module, "get_remote_execution_backend", lambda backend_id: remote)
    monkeypatch.setenv("RUNPOD_API_KEY", "present")
    clip, caption = _clip_fixture(tmp_path)
    manifest = _flat_manifest(tmp_path / "manifest.json", clip, caption)
    run_dir = tmp_path / "failed-live-run"
    config_path = _training_config(tmp_path / "config.json", manifest_path=manifest.name, output_run_dir=run_dir.name)

    rc = training_run_main(["--config", str(config_path), "--out", str(run_dir), "--confirm-spend", "--json"])

    assert rc == 2
    assert compute.teardown_calls == []
    state = json.loads((run_dir / "last_run.json").read_text(encoding="utf-8"))
    assert state["status"] == "FAILED_RECOVERABLE"
    assert state["phase"] == "failed"
    assert state["pod"]["id"] == "pod-live"
    assert state["recoverability"]["manual_recovery_required"] is True
    assert state["recoverability"]["unsafe_teardown_reason"] == "training_may_have_remote_artifacts"
    assert state["recoverability"]["teardown_guard"]["required"] is True
    assert state["final_error"]["phase"] == "training"


def test_training_run_live_missing_handle_path_writes_recoverable_state_without_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compute = MockLiveCompute(tmp_path, include_handle_path=False)
    remote = MockLiveRemote()
    monkeypatch.setattr(training_run_module, "get_compute_backend", lambda backend_id: compute)
    monkeypatch.setattr(training_run_module, "get_remote_execution_backend", lambda backend_id: remote)
    monkeypatch.setenv("RUNPOD_API_KEY", "present")
    clip, caption = _clip_fixture(tmp_path)
    manifest = _flat_manifest(tmp_path / "manifest.json", clip, caption)
    run_dir = tmp_path / "recoverable-live-run"
    config_path = _training_config(tmp_path / "config.json", manifest_path=manifest.name, output_run_dir=run_dir.name)

    rc = training_run_main(["--config", str(config_path), "--out", str(run_dir), "--confirm-spend", "--json"])

    assert rc == 2
    assert compute.teardown_calls == []
    assert remote.exec_calls == []
    state = json.loads((run_dir / "last_run.json").read_text(encoding="utf-8"))
    assert state["status"] == "FAILED_RECOVERABLE"
    assert state["phase"] == "failed"
    assert state["recoverability"]["manual_recovery_required"] is True
    assert state["recoverability"]["unsafe_teardown_reason"] == "missing_handle_path"
    assert state["recoverability"]["teardown_guard"] == {"required": True, "pod_id": "pod-live", "handle_path": None}


def test_training_run_resume_registers_checkpoint_before_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compute = MockLiveCompute(tmp_path)
    remote = MockLiveRemote()
    monkeypatch.setattr(training_run_module, "get_compute_backend", lambda backend_id: compute)
    monkeypatch.setattr(training_run_module, "get_remote_execution_backend", lambda backend_id: remote)
    monkeypatch.setenv("RUNPOD_API_KEY", "present")
    clip, caption = _clip_fixture(tmp_path)
    manifest = _flat_manifest(tmp_path / "manifest.json", clip, caption)
    run_dir = tmp_path / "register-run"
    config_path = _training_config(tmp_path / "config.json", manifest_path=manifest.name, output_run_dir=run_dir.name)
    assert training_run_main(["--config", str(config_path), "--out", str(run_dir), "--confirm-spend", "--json"]) == 0
    live_pull_count = len(remote.pull_calls)

    rc = training_run_main(["resume", "--out", str(run_dir), "--pick", "final", "--notes", "best checkpoint", "--json"])

    assert rc == 0
    assert len(remote.pull_calls) == live_pull_count + 1
    assert remote.pull_calls[-1]["remote_paths"] == ["/workspace/output/demo-final.safetensors"]
    assert len(compute.teardown_calls) == 1
    state = json.loads((run_dir / "last_run.json").read_text(encoding="utf-8"))
    assert state["status"] == "REGISTERED"
    assert state["phase"] == "completed"
    assert state["registration"]["chosen_checkpoint"]["label"] == "final"
    assert state["registration"]["notes"] == "best checkpoint"
    assert Path(state["registration"]["registered_lora_path"]).is_file()
    assert Path(state["registration"]["metadata_path"]).is_file()
    assert state["teardown"] == {"skipped": False, "completed": True, "pod_id": "pod-live"}
    assert state["recoverability"]["teardown_guard"]["required"] is False
    assert "pod" not in state


def test_training_run_resume_skip_teardown_keeps_recovery_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compute = MockLiveCompute(tmp_path)
    remote = MockLiveRemote()
    monkeypatch.setattr(training_run_module, "get_compute_backend", lambda backend_id: compute)
    monkeypatch.setattr(training_run_module, "get_remote_execution_backend", lambda backend_id: remote)
    monkeypatch.setenv("RUNPOD_API_KEY", "present")
    clip, caption = _clip_fixture(tmp_path)
    manifest = _flat_manifest(tmp_path / "manifest.json", clip, caption)
    run_dir = tmp_path / "skip-teardown-run"
    config_path = _training_config(tmp_path / "config.json", manifest_path=manifest.name, output_run_dir=run_dir.name)
    assert training_run_main(["--config", str(config_path), "--out", str(run_dir), "--confirm-spend", "--json"]) == 0

    rc = training_run_main(["resume", "--out", str(run_dir), "--pick", "100", "--notes", "keep pod", "--skip-teardown", "--json"])

    assert rc == 0
    assert compute.teardown_calls == []
    state = json.loads((run_dir / "last_run.json").read_text(encoding="utf-8"))
    assert state["status"] == "REGISTERED"
    assert state["teardown"]["skipped"] is True
    assert state["pod"]["id"] == "pod-live"
    assert state["recoverability"]["teardown_guard"]["required"] is True
    assert Path(state["artifacts"]["registered_lora_path"]).is_file()


def test_training_run_resume_requires_paused_state_and_valid_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compute = MockLiveCompute(tmp_path)
    remote = MockLiveRemote()
    monkeypatch.setattr(training_run_module, "get_compute_backend", lambda backend_id: compute)
    monkeypatch.setattr(training_run_module, "get_remote_execution_backend", lambda backend_id: remote)
    monkeypatch.setenv("RUNPOD_API_KEY", "present")
    clip, caption = _clip_fixture(tmp_path)
    manifest = _flat_manifest(tmp_path / "manifest.json", clip, caption)
    dry_dir = tmp_path / "dry-state"
    dry_config = _training_config(tmp_path / "dry-config.json", manifest_path=manifest.name, output_run_dir=dry_dir.name)
    assert training_run_main(["--config", str(dry_config), "--out", str(dry_dir), "--dry-run", "--json"]) == 0

    assert training_run_main(["resume", "--out", str(dry_dir), "--pick", "final", "--json"]) == 2

    live_dir = tmp_path / "invalid-pick"
    live_config = _training_config(tmp_path / "live-config.json", manifest_path=manifest.name, output_run_dir=live_dir.name)
    assert training_run_main(["--config", str(live_config), "--out", str(live_dir), "--confirm-spend", "--json"]) == 0
    pull_count = len(remote.pull_calls)
    teardown_count = len(compute.teardown_calls)

    assert training_run_main(["resume", "--out", str(live_dir), "--pick", "missing", "--json"]) == 2
    assert len(remote.pull_calls) == pull_count
    assert len(compute.teardown_calls) == teardown_count


def test_normalize_accepts_dataset_builder_compatibility_flat_manifest(tmp_path: Path) -> None:
    dataset_run = tmp_path / "dataset-run"
    dataset_run.mkdir()
    clip, caption = _clip_fixture(dataset_run)
    _flat_manifest(dataset_run / "ai-toolkit-ltx.manifest.json", clip, caption)

    normalized = seed_from_dataset_run(dataset_run, tmp_path / "training-run")

    assert compatibility_manifest_path(dataset_run) == dataset_run / "ai-toolkit-ltx.manifest.json"
    assert normalized.source_format == "ai-toolkit-ltx-flat"
    assert normalized.normalized_manifest_path == tmp_path / "training-run" / "manifests" / "ai-toolkit-ltx" / "manifest.json"
    assert normalized.normalized_manifest_path.exists()
    state = json.loads((normalized.normalized_manifest_path.parent / "manifest_state.json").read_text(encoding="utf-8"))
    assert state["source_manifest_path"] == str(dataset_run / "ai-toolkit-ltx.manifest.json")


def test_normalize_accepts_canonical_final_manifest_and_flat_clips_manifest(tmp_path: Path) -> None:
    clip, caption = _clip_fixture(tmp_path)
    canonical = tmp_path / "final.manifest.json"
    canonical.write_text(
        json.dumps(
            {
                "created_at": "2026-05-21T00:00:00Z",
                "items": [
                    {
                        "item_id": "clip_001",
                        "media_path": str(clip),
                        "caption_file": str(caption),
                        "bucket": "test",
                        "source_url": "file://clip",
                        "duration_s": 1.0,
                        "content_hash": "0" * 64,
                    }
                ],
            }
        ) + "\n",
        encoding="utf-8",
    )
    flat = _flat_manifest(tmp_path / "flat-clips.json", clip, caption)

    canonical_result = normalize_ai_toolkit_manifest(canonical, tmp_path / "train-canonical")
    flat_result = normalize_ai_toolkit_manifest(flat, tmp_path / "train-flat")

    assert canonical_result.source_format == "canonical-final"
    assert flat_result.source_format == "ai-toolkit-ltx-flat"
    canonical_payload = json.loads(canonical_result.normalized_manifest_path.read_text(encoding="utf-8"))
    assert canonical_payload["clips"][0]["clip_id"] == "clip_001"


def test_last_run_state_writes_atomically_and_tracks_pod_recoverability(tmp_path: Path) -> None:
    run_dir = tmp_path / "training-run"
    state = make_initial_state(
        run_dir=run_dir,
        config_path=tmp_path / "config.json",
        mode="live",
        manifest={"normalized_manifest_path": str(run_dir / "manifests" / "ai-toolkit-ltx" / "manifest.json")},
        secrets={"required_env": ["RUNPOD_API_KEY"], "missing_env": [], "dry_run": False},
        budget={"max_gpu_hours": 2, "max_runpod_spend_usd": 10, "spend_confirmed": True},
        now="2026-05-22T00:00:00Z",
    )

    written = write_last_run_state(run_dir, state, now="2026-05-22T00:00:01Z")
    assert written["state_version"] == 1
    assert (run_dir / "last_run.json").is_file()

    handle_path = run_dir / "provision" / "pod_handle.json"
    pod_ready = record_pod_ready(
        run_dir,
        pod_id="pod-123",
        handle_path=handle_path,
        now="2026-05-22T00:00:02Z",
    )
    assert pod_ready["pod"] == {"id": "pod-123", "handle_path": str(handle_path)}
    guard = pod_ready["recoverability"]["teardown_guard"]
    assert guard == {"required": True, "pod_id": "pod-123", "handle_path": str(handle_path)}
    assert "resume --out" in pod_ready["recoverability"]["resume_command"]

    failed = record_failure(run_dir, phase="training", error=RuntimeError("boom"), now="2026-05-22T00:00:03Z")
    assert failed["status"] == "failed"
    assert failed["final_error"]["message"] == "boom"
    assert failed["recoverability"]["teardown_guard"]["required"] is True

    reread = read_last_run_state(run_dir)
    assert reread["final_error"]["phase"] == "training"
