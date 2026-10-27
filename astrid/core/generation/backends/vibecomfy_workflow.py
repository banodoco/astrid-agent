"""RunPod workflow attachment; the injected owner alone supplies checkout."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, TYPE_CHECKING

from .vibecomfy import VibeComfyBackend


if TYPE_CHECKING:
    from astrid.packs.vibecomfy.production_engine import ProductionRunResult


class VibeComfyWorkflowBackend(VibeComfyBackend):
    def __init__(self, *, endpoint_checkout: Callable[..., ContextManager[Any]] | None = None):
        self._endpoint_checkout = endpoint_checkout

    def workflow(
        self, workflow_path: Path, out_dir: Path, *, task_identity: str,
        attempt_identity: str | None = None,
        expected_execution_identity: str | None = None,
        workflow_input_bindings: Mapping[str, Any] | None = None,
        profile_id: str = "pip_embedded",
        hc03_profile: Mapping[str, Any] | None = None,
        comfy_url: str = "", comfy_output_dir: str = "",
    ) -> ProductionRunResult:
        from astrid.packs.vibecomfy.production_engine import (
            ProductionEngineError, run_workflow_result_path,
        )

        if self._endpoint_checkout is None:
            raise ProductionEngineError("RunPod workflow endpoint_checkout integration is unavailable (pending38)")
        if profile_id != "checkout_server" or hc03_profile is None or comfy_url or comfy_output_dir:
            raise ProductionEngineError("RunPod workflow requires the original checkout readiness binding")
        if not expected_execution_identity or not attempt_identity:
            raise ProductionEngineError("RunPod workflow requires original execution and attempt identities")
        return run_workflow_result_path(
            workflow_path, out_dir, task_identity=task_identity,
            attempt_identity=attempt_identity,
            expected_execution_identity=expected_execution_identity,
            workflow_input_bindings=workflow_input_bindings,
            profile_id=profile_id, hc03_profile=hc03_profile,
            endpoint_checkout=self._endpoint_checkout,
        )
