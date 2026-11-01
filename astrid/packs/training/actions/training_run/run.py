"""Runtime shell for the generic ``training.training_run`` orchestrator."""

from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint('training.training_run')
import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from astrid.core._shared.jsonio import write_json_atomic
from astrid.core._shared.result_manifest import build_manifest
from astrid.core.contracts.errors import AstridError, render_astrid_error
from astrid.core.contracts.run_status import RunStatus
from astrid.packs.training.shared.interfaces import ComputeHandle, RunPodHandle

from .ai_toolkit import register as ai_toolkit_register
from .ai_toolkit import review as ai_toolkit_review
from .ai_toolkit import stage as ai_toolkit_stage
from .ai_toolkit import train as ai_toolkit_train
from .compute_backends import get_compute_backend, get_remote_execution_backend
from .config import (
    TrainingRunBudgetError,
    TrainingRunConfigError,
    TrainingRunSecretError,
    TrainingRunSpendConfirmationError,
    load_training_run_config,
    preflight_training_run,
)
from .manifest_input import TrainingManifestError, normalize_ai_toolkit_manifest
from .state import make_initial_state, read_last_run_state, write_last_run_state
from .trainer_adapters import get_trainer_adapter


class TrainingRunShellError(RuntimeError):
    """Raised when the shell cannot prepare a training run."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m astrid.packs.training.actions.training_run.run",
        description="Run or resume a generic built-in training run.",
    )
    _add_run_arguments(parser)
    parser.set_defaults(handler=_cmd_run)
    return parser


def build_resume_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m astrid.packs.training.actions.training_run.run resume",
        description="Resume a persisted generic built-in training run.",
    )
    parser.add_argument("--out", required=True, help="Training run output directory.")
    parser.add_argument("--dry-run", action="store_true", help="Report resume state without executing.")
    parser.add_argument("--pick", help="Checkpoint label, step, basename, or remote path to register.")
    parser.add_argument("--notes", default="", help="Human notes to persist with the registered checkpoint.")
    parser.add_argument("--skip-teardown", action="store_true", help="Register without tearing down the remote pod.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.set_defaults(handler=_cmd_resume)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if raw_argv[:1] == ["resume"]:
        parser = build_resume_parser()
        args = parser.parse_args(raw_argv[1:])
    else:
        parser = build_parser()
        args = parser.parse_args(raw_argv)
    try:
        return int(args.handler(args))
    except (
        TrainingRunBudgetError,
        TrainingRunConfigError,
        TrainingRunSecretError,
        TrainingRunSpendConfirmationError,
        TrainingManifestError,
        TrainingRunShellError,
    ) as exc:
        return render_astrid_error(
            AstridError(
                str(exc),
                recovery_command="python3 -m astrid.packs.training.actions.training_run.run --config <config> --dry-run",
            )
        )


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, help="Training-run config JSON or YAML.")
    parser.add_argument("--out", help="Output directory override; defaults to config output.run_dir.")
    parser.add_argument("--manifest", help="Manifest override; defaults to config manifest_path.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report without provisioning.")
    parser.add_argument("--smoke", action="store_true", help="Run local-only smoke validation without provisioning.")
    parser.add_argument("--confirm-spend", action="store_true", help="Confirm live spend caps after reviewing dry-run output.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")


def _cmd_run(args: argparse.Namespace) -> int:
    parsed = load_training_run_config(args.config)
    config = parsed.data
    run_dir = _resolve_run_dir(args.out, parsed)
    manifest_path = Path(args.manifest).expanduser().resolve() if args.manifest else parsed.manifest_path
    local_only = bool(args.dry_run or args.smoke)
    preflight = preflight_training_run(
        config,
        dry_run=local_only,
        spend_confirmed=bool(args.confirm_spend),
    )
    compute_config = dict(config.get("compute", {}))
    backend_id = preflight.budget.backend
    trainer_id = preflight.budget.trainer_id
    compute_backend = get_compute_backend(backend_id)
    remote_backend = get_remote_execution_backend(backend_id)
    trainer_adapter = get_trainer_adapter(trainer_id)
    estimate = compute_backend.estimate_cost({**compute_config, "max_gpu_hours": preflight.budget.max_gpu_hours})
    if float(estimate.estimated_cost_usd) > float(preflight.budget.max_runpod_spend_usd):
        raise TrainingRunBudgetError(
            "estimated RunPod cost "
            f"${estimate.estimated_cost_usd:.2f} exceeds configured max_runpod_spend_usd "
            f"${preflight.budget.max_runpod_spend_usd:.2f}"
        )

    normalized = normalize_ai_toolkit_manifest(manifest_path, run_dir)
    trainer_config_path = run_dir / "trainer" / trainer_id / "config.yaml"
    trainer_config = _trainer_config(config, trainer_config_path=trainer_config_path)
    built_config_path = trainer_adapter.build_config(normalized.normalized_manifest_path, trainer_config)
    cost_path = _write_planned_cost(
        run_dir,
        backend_id=backend_id,
        trainer_id=trainer_id,
        estimate=estimate,
        max_runpod_spend_usd=preflight.budget.max_runpod_spend_usd,
        remote_capabilities=_provider_capabilities_payload(remote_backend.capabilities),
    )
    state = make_initial_state(
        run_dir=run_dir,
        config_path=parsed.path,
        mode="smoke" if args.smoke else ("dry-run" if args.dry_run else "live"),
        manifest={
            "source_manifest_path": str(normalized.source_manifest_path),
            "normalized_manifest_path": str(normalized.normalized_manifest_path),
            "source_format": normalized.source_format,
        },
        secrets=asdict(preflight.secrets),
        budget=asdict(preflight.budget),
    )
    state["trainer"] = {
        "trainer_id": trainer_id,
        "config_path": str(built_config_path),
    }
    state["compute"] = {
        "backend": backend_id,
        "estimated_cost": _cost_estimate_payload(estimate),
        "planned_cost_path": str(cost_path),
        "remote_capabilities": _provider_capabilities_payload(remote_backend.capabilities),
    }
    state["artifacts"] = {
        "normalized_manifest_path": str(normalized.normalized_manifest_path),
        "trainer_config_path": str(built_config_path),
        "planned_cost_path": str(cost_path),
    }
    state["status"] = "preflight_ready" if local_only else RunStatus.BLOCKED.value
    state["phase"] = "preflight_ready"

    if not local_only:
        state["status"] = "preflight_ready"
        state["phase_history"] = ["preflight_ready"]
        write_last_run_state(run_dir, state)
        final_state = _run_live_training(
            run_dir=run_dir,
            config=config,
            compute_config=compute_config,
            compute_backend=compute_backend,
            remote_backend=remote_backend,
            normalized_manifest_path=normalized.normalized_manifest_path,
            trainer_config_path=built_config_path,
        )
        _emit(final_state, json_output=bool(args.json))
        return 0 if final_state.get("status") == "PAUSED" else 2

    write_last_run_state(run_dir, state)
    _emit(state, json_output=bool(args.json))
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    run_dir = Path(args.out).expanduser().resolve()
    state = read_last_run_state(run_dir)
    payload = build_manifest(
        kind="training_run_resume",
        inputs={"run_dir": str(run_dir), "previous_state_path": str(run_dir / "last_run.json")},
        outputs=[],
        created=datetime.now(timezone.utc).isoformat(),
        schema_version=1,
        status="resume_ready" if args.dry_run else RunStatus.BLOCKED.value,
        mode="resume-dry-run" if args.dry_run else "resume",
        run_dir=str(run_dir),
        previous_state_path=str(run_dir / "last_run.json"),
        previous_status=state.get("status"),
        recoverability=state.get("recoverability"),
    )
    if args.dry_run:
        _emit(payload, json_output=True)
        return 0
    final_state = _resume_register_checkpoint(
        run_dir=run_dir,
        state=state,
        pick=args.pick,
        notes=args.notes,
        skip_teardown=bool(args.skip_teardown),
    )
    _emit(final_state, json_output=bool(args.json))
    return 0


def _resume_register_checkpoint(
    *,
    run_dir: Path,
    state: dict[str, Any],
    pick: str | None,
    notes: str,
    skip_teardown: bool,
) -> dict[str, Any]:
    if state.get("status") != "PAUSED" or state.get("phase") != "review_ready":
        raise TrainingRunShellError("resume requires a PAUSED training run at the checkpoint review gate")
    if not pick:
        raise TrainingRunShellError("resume requires --pick <checkpoint>")

    config = load_training_run_config(state["config_path"]).data
    compute_config = dict(config.get("compute", {}))
    backend_id = str(compute_config.get("backend") or state.get("compute", {}).get("backend") or "runpod")
    compute_backend = get_compute_backend(backend_id)
    remote_backend = get_remote_execution_backend(backend_id)
    handle = _handle_from_state(state, compute_config)
    checkpoint_manifest = _checkpoint_manifest_path(run_dir, state)
    checkpoint = _select_checkpoint(ai_toolkit_train.parse_checkpoint_manifest(checkpoint_manifest), pick)

    _record_runtime_phase(run_dir, "registering", handle=handle)
    registration = ai_toolkit_register.register_checkpoint(
        remote_backend,
        handle,
        checkpoint_remote_path=checkpoint.remote_path,
        local_dir=run_dir / "checkpoints" / "selected",
        registry_dir=run_dir / "registered",
        lora_id=_lora_id(config),
        metadata={
            "notes": notes,
            "checkpoint": {
                "label": checkpoint.label,
                "step": checkpoint.step,
                "remote_path": checkpoint.remote_path,
            },
        },
        produces_dir=run_dir / "remote" / "register",
        ssh_key=_optional_path(compute_config.get("ssh_key")),
    )

    teardown_receipt: dict[str, Any]
    if skip_teardown:
        teardown_receipt = {"skipped": True, "reason": "operator_requested_skip_teardown"}
    else:
        try:
            _record_runtime_phase(run_dir, "tearing_down", handle=handle)
            compute_backend.teardown(handle)
            teardown_receipt = {"skipped": False, "completed": True, "pod_id": handle.pod_id}
        except Exception as exc:
            return _write_recoverable_failure(
                run_dir,
                phase="tearing_down",
                error=exc,
                handle=handle,
                unsafe_reason="teardown_failed_after_registration",
            )
    return _write_registered_state(
        run_dir,
        handle=handle,
        checkpoint=checkpoint,
        registration=registration,
        notes=notes,
        teardown_receipt=teardown_receipt,
        keep_pod=skip_teardown,
    )


def _handle_from_state(state: Mapping[str, Any], compute_config: Mapping[str, Any]) -> RunPodHandle:
    pod = state.get("pod") if isinstance(state.get("pod"), Mapping) else {}
    pod_id = str(pod.get("id") or "")
    handle_path = str(pod.get("handle_path") or "")
    if not pod_id or not handle_path:
        raise TrainingRunShellError("paused state is missing pod id or handle path")
    return RunPodHandle(
        pod_id,
        str(compute_config.get("gpu_type") or "NVIDIA RTX 6000 Ada Generation"),
        metadata={"handle_path": handle_path},
    )


def _checkpoint_manifest_path(run_dir: Path, state: Mapping[str, Any]) -> Path:
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), Mapping) else {}
    path = Path(str(artifacts.get("checkpoint_manifest_path") or run_dir / "checkpoints" / "checkpoint_manifest.json"))
    if not path.is_file():
        raise TrainingRunShellError(f"checkpoint manifest not found: {path}")
    return path


def _select_checkpoint(checkpoints: list[ai_toolkit_train.Checkpoint], pick: str) -> ai_toolkit_train.Checkpoint:
    for checkpoint in checkpoints:
        options = {
            checkpoint.remote_path,
            Path(checkpoint.remote_path).name,
            Path(checkpoint.remote_path).stem,
            str(checkpoint.step) if checkpoint.step is not None else "",
            checkpoint.label or "",
        }
        if pick in options:
            return checkpoint
    available = ", ".join(str(checkpoint.label or checkpoint.step or Path(checkpoint.remote_path).name) for checkpoint in checkpoints)
    raise TrainingRunShellError(f"unknown checkpoint pick {pick!r}; available: {available}")


def _write_registered_state(
    run_dir: Path,
    *,
    handle: RunPodHandle,
    checkpoint: ai_toolkit_train.Checkpoint,
    registration: ai_toolkit_register.RegistrationResult,
    notes: str,
    teardown_receipt: Mapping[str, Any],
    keep_pod: bool,
) -> dict[str, Any]:
    state = read_last_run_state(run_dir)
    history = list(state.get("phase_history", []))
    if "completed" not in history:
        history.append("completed")
    state["phase_history"] = history
    state["phase"] = "completed"
    state["status"] = "REGISTERED"
    state["registration"] = {
        "chosen_checkpoint": {
            "label": checkpoint.label,
            "step": checkpoint.step,
            "remote_path": checkpoint.remote_path,
        },
        "pulled_checkpoint_path": str(registration.pulled_checkpoint_path),
        "registered_lora_path": str(registration.registered_lora_path),
        "metadata_path": str(registration.metadata_path),
        "notes": notes,
        "metadata": {
            "lora_id": registration.lora_id,
            "pull_remote_paths": list(registration.pull_result.remote_paths),
        },
    }
    state["teardown"] = dict(teardown_receipt)
    state["artifacts"] = _merge_artifacts(
        state.get("artifacts", {}),
        {
            "registered_lora_path": str(registration.registered_lora_path),
            "registered_lora_metadata_path": str(registration.metadata_path),
        },
    )
    if keep_pod:
        _attach_pod_recoverability(state, handle)
    else:
        state.pop("pod", None)
        state.setdefault("recoverability", {})["teardown_guard"] = {
            "required": False,
            "pod_id": None,
            "handle_path": None,
        }
    return write_last_run_state(run_dir, state)


def _resolve_run_dir(out: str | None, parsed: Any) -> Path:
    if out:
        return Path(out).expanduser().resolve()
    return parsed.run_dir


def _trainer_config(config: dict[str, Any], *, trainer_config_path: Path) -> dict[str, Any]:
    output = dict(config.get("output", {}))
    trainer_config = dict(config)
    trainer_config["config_path"] = str(trainer_config_path)
    trainer_config.setdefault("checkpoint", {})
    trainer_config["output"] = output
    trainer_config.setdefault("dataset_dir", "/workspace/dataset")
    trainer_config.setdefault("output_dir", output.get("remote_output_dir") or "/workspace/output")
    return trainer_config


def _run_live_training(
    *,
    run_dir: Path,
    config: dict[str, Any],
    compute_config: dict[str, Any],
    compute_backend: Any,
    remote_backend: Any,
    normalized_manifest_path: Path,
    trainer_config_path: Path,
) -> dict[str, Any]:
    handle: ComputeHandle | None = None
    phase = "provisioning"
    try:
        _record_runtime_phase(run_dir, "provisioning")
        handle = compute_backend.provision(_provision_config(run_dir, compute_config))
        handle_path = _handle_path(handle)
        if not handle_path:
            return _write_recoverable_failure(
                run_dir,
                phase="provisioning",
                error="provisioned handle did not include a pod handle path; teardown is unsafe",
                handle=handle,
                unsafe_reason="missing_handle_path",
            )
        _record_runtime_phase(
            run_dir,
            "pod_ready",
            handle=handle,
            extra={"pod_ready": {"id": handle.pod_id, "handle_path": str(handle_path)}},
        )

        phase = "staging"
        _record_runtime_phase(run_dir, "staging", handle=handle)
        remote_root = _remote_root(config)
        ai_toolkit_stage.stage_training_inputs(
            remote_backend,
            handle,
            manifest_path=normalized_manifest_path,
            trainer_config_path=trainer_config_path,
            local_root=run_dir,
            remote_root=remote_root,
            upload_mode=str(compute_config.get("upload_mode") or "sftp_walk"),
            excludes=_split_excludes(compute_config.get("excludes")),
            produces_dir=run_dir / "remote" / "stage",
            timeout=_optional_int(compute_config.get("stage_timeout_seconds")),
        )

        phase = "training"
        _record_runtime_phase(run_dir, "training", handle=handle)
        remote_config_path = f"{remote_root.rstrip('/')}/trainer/ai-toolkit-ltx/config.yaml"
        remote_output_dir = _remote_output_dir(config)
        training = ai_toolkit_train.run_training(
            remote_backend,
            handle,
            remote_config_path=remote_config_path,
            remote_output_dir=remote_output_dir,
            log_path=run_dir / "logs" / "training.log",
            produces_dir=run_dir / "remote" / "train",
            timeout=_optional_int(compute_config.get("training_timeout_seconds")),
        )
        checkpoint_manifest_path = _write_checkpoint_manifest(
            run_dir,
            config=config,
            remote_output_dir=remote_output_dir,
        )
        checkpoints = ai_toolkit_train.parse_checkpoint_manifest(checkpoint_manifest_path)

        phase = "pulling_artifacts"
        _record_runtime_phase(
            run_dir,
            "pulling_artifacts",
            handle=handle,
            extra={"training": {"log_path": str(training.log_path), "checkpoint_manifest_path": str(checkpoint_manifest_path)}},
        )
        review = ai_toolkit_review.generate_review_samples(
            remote_backend,
            handle,
            checkpoints=checkpoints,
            prompts=_sample_prompts(config),
            local_dir=run_dir / "review",
            title=_review_title(config),
            rubric=_review_labels(config),
            remote_output_dir=remote_output_dir,
            produces_dir=run_dir / "remote" / "review",
            ssh_key=_optional_path(compute_config.get("ssh_key")),
            timeout=_optional_int(compute_config.get("review_timeout_seconds")),
        )
        return _record_runtime_phase(
            run_dir,
            "review_ready",
            handle=handle,
            extra={
                "status": "PAUSED",
                "review": {
                    "index_path": str(review.index_path),
                    "sample_paths": [str(sample.local_path) for sample in review.samples],
                    "human_gate": "checkpoint_review",
                    "resume_command": f"python3 -m astrid.packs.training.actions.training_run.run resume --out {run_dir}",
                },
                "artifacts": _merge_artifacts(
                    read_last_run_state(run_dir).get("artifacts", {}),
                    {
                        "training_log_path": str(training.log_path),
                        "checkpoint_manifest_path": str(checkpoint_manifest_path),
                        "review_index_path": str(review.index_path),
                    },
                ),
            },
        )
    except Exception as exc:
        return _handle_post_provision_failure(
            run_dir,
            phase=phase,
            error=exc,
            handle=handle,
            compute_backend=compute_backend,
        )


def _handle_post_provision_failure(
    run_dir: Path,
    *,
    phase: str,
    error: BaseException,
    handle: ComputeHandle | None,
    compute_backend: Any,
) -> dict[str, Any]:
    if handle is None:
        return _write_failed_state(run_dir, phase=phase, error=error)
    handle_path = _handle_path(handle)
    if not handle_path:
        return _write_recoverable_failure(
            run_dir,
            phase=phase,
            error=error,
            handle=handle,
            unsafe_reason="missing_handle_path",
        )
    if _phase_requires_checkpoint_recovery(phase):
        return _write_recoverable_failure(
            run_dir,
            phase=phase,
            error=error,
            handle=handle,
            unsafe_reason=f"{phase}_may_have_remote_artifacts",
        )
    try:
        compute_backend.teardown(handle)
    except Exception as teardown_error:
        return _write_recoverable_failure(
            run_dir,
            phase=phase,
            error=error,
            handle=handle,
            teardown_error=teardown_error,
            unsafe_reason="teardown_failed",
        )
    return _write_failed_state(run_dir, phase=phase, error=error, teardown_completed=True)


def _phase_requires_checkpoint_recovery(phase: str) -> bool:
    return phase in {"training", "pulling_artifacts", "registering", "tearing_down"}


def _record_runtime_phase(
    run_dir: Path,
    phase: str,
    *,
    handle: ComputeHandle | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state = read_last_run_state(run_dir)
    history = list(state.get("phase_history", []))
    if not history or history[-1] != phase:
        history.append(phase)
    state["phase_history"] = history
    state["phase"] = phase
    state["status"] = phase
    if handle is not None:
        _attach_pod_recoverability(state, handle)
    if extra:
        state.update(dict(extra))
    return write_last_run_state(run_dir, state)


def _write_failed_state(
    run_dir: Path,
    *,
    phase: str,
    error: BaseException,
    teardown_completed: bool = False,
) -> dict[str, Any]:
    state = read_last_run_state(run_dir)
    history = list(state.get("phase_history", []))
    history.append("tearing_down" if teardown_completed else "failed")
    state["phase_history"] = history
    state["phase"] = "failed"
    state["status"] = "failed"
    state["final_error"] = _error_payload(error, phase=phase)
    if teardown_completed:
        state.pop("pod", None)
        state.setdefault("recoverability", {})["teardown_guard"] = {
            "required": False,
            "pod_id": None,
            "handle_path": None,
        }
        state["teardown"] = {"completed": True}
    return write_last_run_state(run_dir, state)


def _write_recoverable_failure(
    run_dir: Path,
    *,
    phase: str,
    error: BaseException | str,
    handle: ComputeHandle | None,
    unsafe_reason: str,
    teardown_error: BaseException | None = None,
) -> dict[str, Any]:
    state = read_last_run_state(run_dir)
    history = list(state.get("phase_history", []))
    history.append("failed")
    state["phase_history"] = history
    state["phase"] = "failed"
    state["status"] = "FAILED_RECOVERABLE"
    state["final_error"] = _error_payload(error, phase=phase)
    if teardown_error is not None:
        state["final_error"]["teardown_error"] = _error_payload(teardown_error, phase="tearing_down")
    pod_id = getattr(handle, "pod_id", None) if handle is not None else None
    handle_path = _handle_path(handle) if handle is not None else None
    if handle is not None and handle_path is not None:
        _attach_pod_recoverability(state, handle)
    else:
        state.setdefault("recoverability", {})["teardown_guard"] = {
            "required": True,
            "pod_id": pod_id,
            "handle_path": str(handle_path) if handle_path else None,
        }
    state.setdefault("recoverability", {})["unsafe_teardown_reason"] = unsafe_reason
    state["recoverability"]["manual_recovery_required"] = True
    return write_last_run_state(run_dir, state)


def _attach_pod_recoverability(state: dict[str, Any], handle: ComputeHandle) -> None:
    handle_path = _handle_path(handle)
    if handle_path is None:
        return
    state["pod"] = {"id": handle.pod_id, "handle_path": str(handle_path)}
    state.setdefault("recoverability", {})["teardown_guard"] = {
        "required": True,
        "pod_id": handle.pod_id,
        "handle_path": str(handle_path),
    }


def _error_payload(error: BaseException | str, *, phase: str) -> dict[str, Any]:
    if isinstance(error, BaseException):
        return {"phase": phase, "type": type(error).__name__, "message": str(error)}
    return {"phase": phase, "type": "error", "message": str(error)}


def _provision_config(run_dir: Path, compute_config: dict[str, Any]) -> dict[str, Any]:
    config = dict(compute_config)
    config.setdefault("produces_dir", run_dir / "provision")
    config.setdefault("run_dir", run_dir)
    return config


def _handle_path(handle: ComputeHandle | None) -> Path | None:
    if handle is None:
        return None
    metadata = getattr(handle, "metadata", {}) or {}
    value = metadata.get("handle_path") if isinstance(metadata, Mapping) else None
    return Path(str(value)).expanduser().resolve() if value else None


def _remote_root(config: Mapping[str, Any]) -> str:
    output = config.get("output") if isinstance(config.get("output"), Mapping) else {}
    return str(output.get("remote_root") or "/workspace")


def _remote_output_dir(config: Mapping[str, Any]) -> str:
    output = config.get("output") if isinstance(config.get("output"), Mapping) else {}
    return str(output.get("remote_output_dir") or "/workspace/output")


def _sample_prompts(config: Mapping[str, Any]) -> list[str]:
    checkpoint = config.get("checkpoint") if isinstance(config.get("checkpoint"), Mapping) else {}
    prompts = checkpoint.get("sample_prompts") if isinstance(checkpoint, Mapping) else []
    return [str(prompt) for prompt in prompts]


def _review_labels(config: Mapping[str, Any]) -> list[str]:
    checkpoint = config.get("checkpoint") if isinstance(config.get("checkpoint"), Mapping) else {}
    labels = checkpoint.get("review_labels") if isinstance(checkpoint, Mapping) else []
    return [str(label) for label in labels]


def _review_title(config: Mapping[str, Any]) -> str:
    lora = config.get("lora_config") if isinstance(config.get("lora_config"), Mapping) else {}
    lora_id = str(lora.get("lora_id") or "training-run")
    return f"{lora_id} checkpoint review"


def _lora_id(config: Mapping[str, Any]) -> str:
    lora = config.get("lora_config") if isinstance(config.get("lora_config"), Mapping) else {}
    return str(lora.get("lora_id") or "lora")


def _write_checkpoint_manifest(run_dir: Path, *, config: Mapping[str, Any], remote_output_dir: str) -> Path:
    lora = config.get("lora_config") if isinstance(config.get("lora_config"), Mapping) else {}
    lora_id = str(lora.get("lora_id") or "lora")
    step = int(lora.get("steps") or 0)
    path = run_dir / "checkpoints" / "checkpoint_manifest.json"
    payload = build_manifest(
        kind="training_run_checkpoint",
        inputs={},
        outputs=[],
        created=datetime.now(timezone.utc).isoformat(),
        schema_version=1,
        checkpoints=[
            {
                "label": "final",
                "step": step,
                "remote_path": f"{remote_output_dir.rstrip('/')}/{lora_id}-final.safetensors",
            }
        ],
    )
    write_json_atomic(path, payload)
    return path


def _split_excludes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _optional_int(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def _optional_path(value: Any) -> Path | None:
    return Path(str(value)).expanduser().resolve() if value else None


def _merge_artifacts(existing: Any, updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing) if isinstance(existing, Mapping) else {}
    merged.update(dict(updates))
    return merged


def _write_planned_cost(
    run_dir: Path,
    *,
    backend_id: str,
    trainer_id: str,
    estimate: Any,
    max_runpod_spend_usd: float,
    remote_capabilities: dict[str, Any],
) -> Path:
    path = run_dir / "planned_cost.json"
    payload = build_manifest(
        kind="training_run_planned_cost",
        inputs={},
        outputs=[],
        created=datetime.now(timezone.utc).isoformat(),
        schema_version=1,
        backend=backend_id,
        trainer_id=trainer_id,
        estimate=_cost_estimate_payload(estimate),
        max_runpod_spend_usd=max_runpod_spend_usd,
        within_budget=float(estimate.estimated_cost_usd) <= float(max_runpod_spend_usd),
        remote_capabilities=remote_capabilities,
    )
    write_json_atomic(path, payload)
    return path


def _provider_capabilities_payload(capabilities: Any) -> dict[str, Any]:
    return {
        "backend": capabilities.backend,
        "supports_exec": capabilities.supports_exec,
        "supports_artifact_pull": capabilities.supports_artifact_pull,
        "supports_artifact_push": capabilities.supports_artifact_push,
        "supports_cost_estimate": capabilities.supports_cost_estimate,
        "metadata": dict(capabilities.metadata),
    }


def _cost_estimate_payload(estimate: Any) -> dict[str, Any]:
    return {
        "gpu_hours": estimate.gpu_hours,
        "estimated_cost_usd": estimate.estimated_cost_usd,
        "backend": estimate.backend,
        "details": dict(estimate.details),
    }


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"status: {payload['status']}")
    print(f"run_dir: {payload['run_dir']}")
    manifest = payload.get("manifest")
    if isinstance(manifest, dict):
        print(f"manifest: {manifest.get('normalized_manifest_path')}")
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        print(f"trainer_config: {artifacts.get('trainer_config_path')}")
        print(f"planned_cost: {artifacts.get('planned_cost_path')}")
    secrets = payload.get("secrets")
    if isinstance(secrets, dict) and secrets.get("missing_env"):
        print("missing_env: " + ", ".join(secrets["missing_env"]))


if __name__ == "__main__":
    raise SystemExit(main())
