"""Optional workflow operation; model adapters keep their existing API."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, TYPE_CHECKING, runtime_checkable

if TYPE_CHECKING:
    from astrid.packs.vibecomfy.production_engine import ProductionRunResult


@runtime_checkable
class WorkflowBackendAdapter(Protocol):
    def workflow(
        self, workflow_path: Path, out_dir: Path, *, task_identity: str,
        attempt_identity: str | None = None,
        expected_execution_identity: str | None = None,
        workflow_input_bindings: Mapping[str, Any] | None = None,
        profile_id: str = "pip_embedded",
        hc03_profile: Mapping[str, Any] | None = None,
        comfy_url: str = "", comfy_output_dir: str = "",
    ) -> ProductionRunResult: ...
