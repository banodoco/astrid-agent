"""H3 workflow backend: local engine endpoint, or RunPod via the injected checkout owner."""
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

        if profile_id == "local_server":
            # Local placement: the endpoint record supplies the loopback URL and
            # the engine's ComfyUI output directory; no readiness profile applies.
            if self._endpoint_checkout is not None or hc03_profile is not None:
                raise ProductionEngineError("local workflow cannot use a RunPod checkout or readiness profile")
            if not comfy_url or not comfy_output_dir:
                raise ProductionEngineError("local workflow requires comfy_url and comfy_output_dir from the engine endpoint")
            kwargs: dict[str, Any] = {"task_identity": task_identity}
            if attempt_identity is not None:
                kwargs["attempt_identity"] = attempt_identity
            if expected_execution_identity is not None:
                kwargs["expected_execution_identity"] = expected_execution_identity
            if workflow_input_bindings is not None:
                kwargs["workflow_input_bindings"] = workflow_input_bindings
            return run_workflow_result_path(
                workflow_path, out_dir, profile_id="local_server",
                comfy_url=comfy_url, comfy_output_dir=comfy_output_dir, **kwargs,
            )
        if self._endpoint_checkout is None:
            # Local placement: embedded and host-session checkout profiles keep
            # the base behavior; the RunPod route always injects a checkout.
            return super().workflow(
                workflow_path, out_dir, task_identity=task_identity,
                attempt_identity=attempt_identity,
                expected_execution_identity=expected_execution_identity,
                workflow_input_bindings=workflow_input_bindings,
                profile_id=profile_id, hc03_profile=hc03_profile,
                comfy_url=comfy_url, comfy_output_dir=comfy_output_dir,
            )
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
