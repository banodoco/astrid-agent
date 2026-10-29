"""G02 terminal receipt serialization shared by host and local orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from astrid.core._shared.result_manifest import write_json_atomic
from astrid.sdk.results import InvocationResult


def write_terminal_result(
    root: Path,
    result: InvocationResult,
    *,
    status: str,
    request_digest: str,
    resolved_options: Mapping[str, Any],
    cost: Any,
    warnings: Sequence[str],
    publication: Mapping[str, Any],
    parent_task_id: str | None = None,
    parent_attempt_id: str | None = None,
) -> None:
    """Publish the existing InvocationResult plus G02 manifest/settlement facts."""

    receipt = result.to_dict()
    receipt.update({
        "status": status,
        "task_id": result.kernel_task_id,
        "attempt_id": result.kernel_attempt_id,
        "parent_task_id": parent_task_id,
        "parent_attempt_id": parent_attempt_id,
        "request_digest": request_digest,
        "resolved_options": dict(resolved_options),
        "cost": cost if cost is not None else "unknown",
        "warnings": list(warnings),
        "publication": dict(publication),
    })
    write_json_atomic(root / "result.json", receipt)
