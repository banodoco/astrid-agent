"""Shared G02 mapping for an existing invocation and prepared terminal facts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from astrid.sdk.results import InvocationResult


def serialize_terminal_result(
    result: InvocationResult,
    *,
    status: str,
    task_id: str,
    attempt_id: str | None,
    parent_task_id: str | None,
    parent_attempt_id: str | None,
    request_digest: str,
    resolved_options: Mapping[str, Any],
    cost: Any,
    warnings: list[str],
    settled: bool | None,
    settlement: Any,
    effect: Any,
) -> dict[str, Any]:
    """Map a genuine SDK result and prepared manifest/publication facts to G02.

    The caller supplies the existing manifest path and verified outputs on
    ``result``, along with merged warnings, resolved cost and publication facts.
    This mapping performs no I/O, validation, error coercion or publication;
    those remain the caller's responsibility, including any fallback receipt.
    """
    receipt = result.to_dict()
    receipt.update({
        "status": status, "task_id": task_id, "attempt_id": attempt_id,
        "parent_task_id": parent_task_id,
        "parent_attempt_id": parent_attempt_id,
        "request_digest": request_digest,
        "resolved_options": resolved_options, "cost": cost if cost is not None else "unknown",
        "warnings": warnings,
        "publication": {"status": "settled" if settled else "not_settled",
                        "receipt": settlement, "effect": effect},
    })
    return receipt
