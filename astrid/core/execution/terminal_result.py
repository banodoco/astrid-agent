"""Shared G02 mapping for an existing invocation and prepared terminal facts."""

from __future__ import annotations

import re
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
    pack_provenance: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map a genuine SDK result and prepared manifest/publication facts to G02.

    The caller supplies the existing manifest path and verified outputs on
    ``result``, along with merged warnings, resolved cost and publication facts.
    This mapping performs no I/O, validation, error coercion or publication;
    those remain the caller's responsibility, including any fallback receipt.
    """
    from astrid.sdk.results import _json_safe

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
    if pack_provenance is not None:
        rows = []
        for row in pack_provenance:
            if not isinstance(row, Mapping) or set(row) != {
                "pack_id", "origin", "installed_digest", "observed_digest", "modified",
            }:
                raise ValueError("pack provenance row has invalid fields")
            observed = row["observed_digest"]
            installed = row["installed_digest"]
            if (not isinstance(row["pack_id"], str) or not row["pack_id"]
                    or row["origin"] not in {"default", "user", "folder", "personal"}
                    or not isinstance(observed, str) or re.fullmatch(r"[0-9a-f]{64}", observed) is None
                    or (installed is not None and (not isinstance(installed, str)
                        or re.fullmatch(r"[0-9a-f]{64}", installed) is None))
                    or row["modified"] is not (installed != observed if installed is not None else None)):
                raise ValueError("pack provenance row requires valid installed and observed digests")
            rows.append(dict(row))
        receipt["pack_provenance"] = rows
    # Runtime lifecycle calls return typed SDK DTOs. Preserve their structured
    # settlement evidence before the host writes its terminal receipt.
    return _json_safe(receipt)
