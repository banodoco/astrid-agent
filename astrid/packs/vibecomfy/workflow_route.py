"""The comfyui engine's workflow run: the local engine endpoint, or an attached local server.

The engine's run site (actions/run/run.py _run_and_settle, reached by ComfyUIEngine.execute) constructs
this; it is not a registered backend and nothing selects it by name.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

from astrid.core.generation.backends.vibecomfy import VibeComfyBackend


if TYPE_CHECKING:
    from astrid.packs.vibecomfy.production_engine import ProductionRunResult


class WorkflowRunner(VibeComfyBackend):
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
        from astrid.core.generation.producer_result import (
            complete_producer_result, preserve_result_on_error, source_input_ref, workflow_transport,
        )
        transport = workflow_transport(profile_id, comfy_url)

        if profile_id == "local_server":
            # Attached placement needs only its loopback URL. Output custody
            # uses the selected HTTP client, without a shared output directory.
            if hc03_profile is not None:
                raise ProductionEngineError("local workflow cannot use a readiness profile")
            if not comfy_url:
                raise ProductionEngineError("local workflow requires comfy_url from the engine endpoint")
            kwargs: dict[str, Any] = {"task_identity": task_identity}
            if comfy_output_dir:
                kwargs["comfy_output_dir"] = comfy_output_dir
            if attempt_identity is not None:
                kwargs["attempt_identity"] = attempt_identity
            if expected_execution_identity is not None:
                kwargs["expected_execution_identity"] = expected_execution_identity
            if workflow_input_bindings is not None:
                kwargs["workflow_input_bindings"] = workflow_input_bindings
            source = source_input_ref(workflow_path, out_dir)
            with preserve_result_on_error(out_dir, task_id=task_identity, attempt_id=attempt_identity,
                                          source=source, workflow_path=workflow_path, transport=transport):
                result = run_workflow_result_path(
                    workflow_path, out_dir, profile_id="local_server",
                    comfy_url=comfy_url, **kwargs,
                )
                return complete_producer_result(result, out_dir, task_id=task_identity,
                                                attempt_id=attempt_identity, source=source, transport=transport)
        # Embedded and host-session checkout profiles keep the base behavior.
        return super().workflow(
            workflow_path, out_dir, task_identity=task_identity,
            attempt_identity=attempt_identity,
            expected_execution_identity=expected_execution_identity,
            workflow_input_bindings=workflow_input_bindings,
            profile_id=profile_id, hc03_profile=hc03_profile,
            comfy_url=comfy_url, comfy_output_dir=comfy_output_dir,
        )
